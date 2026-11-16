"""What every operator shares: counters, the frame budget, and temporary files.

The memory rule of the engine is enforced here. All memory is buffer pool frames:

    a pinned page          a table page a scan is reading (pool.fetch_page)
    a reserved frame       working memory an operator asked for (pool.reserve)

Rows an operator keeps in a Python list or dictionary are counted through a Workspace, which
reserves frames first and then refuses any row that would not fit in them, packed exactly as a
slotted page packs rows. A RunWriter or RunReader is one page of a temporary file in memory,
and reserves one frame for it. The pool has B slots in all, so pinned plus reserved can never
exceed B: asking for one more raises instead of quietly using memory nobody counted.
"""
from store import page
from store.disk import PAGE_SIZE
from store.record import RecordCodec, RecordError


class ExecError(Exception):
    """A plan that cannot be run, or that failed while running."""


class BudgetError(ExecError):
    """The plan cannot run in the frames it was given."""


class PlanError(ExecError):
    """A physical plan that is malformed, or that names a table or column that does not exist."""


class Counters:
    __slots__ = ('tuples_out', 'comparisons', 'page_reads', 'page_writes')

    def __init__(self):
        self.tuples_out = self.comparisons = self.page_reads = self.page_writes = 0

    def as_dict(self):
        return {'tuples_out': self.tuples_out, 'comparisons': self.comparisons,
                'page_reads': self.page_reads, 'page_writes': self.page_writes}


class ExecContext:

    def __init__(self, db):
        self.db = db
        self.pool = db.pool
        self.disk = db.disk
        self.catalog = db.catalog


def codec_for(columns):
    return RecordCodec([(c.kind, c.size) for c in columns])


class Workspace:
    """Working memory of `frames` frames for rows held in a list or dictionary.

    The operator keeps the rows; the workspace keeps the count. add(size) answers whether one
    more row of that encoded size still fits. It simulates filling slotted pages one after
    another, so "three frames full" means the same rows that fill three pages on disk."""

    def __init__(self, ctx, frames):
        self.pool = ctx.pool
        self.frames = frames
        self.pool.reserve(frames)
        self.clear()

    def clear(self):
        self.pages = 1
        self.room = page.CAPACITY

    def add(self, size):
        need = size + page.SLOT_SIZE
        if need > page.CAPACITY:
            raise ExecError(f'a row of {size} bytes does not fit in one page')
        if need > self.room:
            if self.pages == self.frames:
                return False
            self.pages += 1
            self.room = page.CAPACITY
        self.room -= need
        return True

    def close(self):
        if self.frames:
            self.pool.release(self.frames)
            self.frames = 0


class RunWriter:
    """Writes rows to a temporary file, one page at a time, through one frame.

    The file is created when the first page is written; close() returns it, or None if no row
    was ever added. With own_frame=False the writer reserves nothing: that is for a caller
    writing out rows its own workspace already accounts for (a sort writing a run)."""

    def __init__(self, ctx, codec, counters, own_frame=True):
        self.ctx = ctx
        self.codec = codec
        self.counters = counters
        self.frames = 1 if own_frame else 0
        if self.frames:
            ctx.pool.reserve(1)
        self.buf = bytearray(PAGE_SIZE)
        page.init(self.buf, 0)
        self.file = None
        self.rows = 0

    def add(self, row):
        try:
            record = self.codec.encode(row)
        except RecordError as e:                # a number too large even for the column it was given
            raise ExecError(str(e)) from None
        if page.insert(self.buf, record) is None:
            self._flush()
            if page.insert(self.buf, record) is None:
                raise ExecError(f'a row of {len(record)} bytes does not fit in one page')
        self.rows += 1

    def _flush(self):
        if self.file is None:
            self.file = self.ctx.disk.create_temp()
        self.file.append_page(bytes(self.buf))
        self.counters.page_writes += 1
        page.init(self.buf, self.file.num_pages)       # the page id of the next page of the file

    def close(self):
        if self.buf is not None:
            if page.slot_count(self.buf):
                self._flush()
            self.buf = None
            if self.frames:
                self.ctx.pool.release(1)
            if self.file is not None:
                self.file.release()
                self.file.rows = self.rows
        return self.file

    def abandon(self):
        """Close and throw away whatever was written."""
        file = self.close()
        if file is not None:
            file.delete()


class RunReader:
    """Reads the rows of a temporary file in order, one page in memory at a time, one frame.

    mark() names the row most recently returned, and seek(mark) makes the next call to next()
    return that row again. Seeking to a row on another page reads that page again."""

    def __init__(self, ctx, file, codec, counters):
        self.ctx = ctx
        self.file = file
        self.codec = codec
        self.counters = counters
        ctx.pool.reserve(1)
        self.open = True
        self.page_no = -1
        self.rows = []
        self.i = 0

    def _load(self, page_no):
        data = self.file.read_page(page_no)
        self.counters.page_reads += 1
        decode = self.codec.decode
        self.rows = [decode(record) for _, record in page.records(data)]
        self.page_no = page_no

    def next(self):
        while self.i >= len(self.rows):
            if self.page_no + 1 >= self.file.num_pages:
                return None
            self._load(self.page_no + 1)
            self.i = 0
        row = self.rows[self.i]
        self.i += 1
        return row

    def mark(self):
        return (self.page_no, self.i - 1)

    def seek(self, mark):
        page_no, i = mark
        if page_no != self.page_no:
            self._load(page_no)
        self.i = i

    def close(self):
        if self.open:
            self.open = False
            self.rows = []
            self.file.release()
            self.ctx.pool.release(1)


def partition(ctx, pull, codec, key_of, count, seed, counters):
    """Split a stream of rows into `count` temporary files by hash of their key, one frame per
    file. Rows with equal keys always land in the same file. Returns one file per partition,
    None for a partition that received no row."""
    writers = []
    try:
        for _ in range(count):
            writers.append(RunWriter(ctx, codec, counters))
        while True:
            row = pull()
            if row is None:
                break
            writers[hash_key(key_of(row), seed) % count].add(row)
    except BaseException:
        for writer in writers:
            writer.abandon()
        raise
    return [writer.close() for writer in writers]


def hash_key(values, seed):
    """A hash of a tuple of column values that is the same in every run of the program (Python's
    own hash of a string is not). FNV-1a over the bytes of the values, started from a different
    basis for each seed, so that seed 0, 1, 2 ... are different hash functions: rows that collide
    under one are spread out again by the next."""
    h = (0xcbf29ce484222325 ^ (seed * 0x9e3779b97f4a7c15)) & 0xffffffffffffffff
    for value in values:
        if isinstance(value, str):
            data = value.encode('utf-8')
        else:
            if isinstance(value, float) and value.is_integer():
                value = int(value)                  # 2 and 2.0 are equal, so they must hash alike
            data = value.to_bytes(8, 'little', signed=True) if isinstance(value, int) else repr(value).encode()
        for byte in data:
            h = ((h ^ byte) * 0x100000001b3) & 0xffffffffffffffff
        h = ((h ^ 0xff) * 0x100000001b3) & 0xffffffffffffffff      # a separator between values
    h ^= h >> 33                                                    # mix the high bits down
    h = (h * 0xff51afd7ed558ccd) & 0xffffffffffffffff
    h ^= h >> 33
    return h
