"""External merge sort.

    pass 0   Fill the workspace with input rows, sort them in memory, write them out as one run.
             Repeat until the input ends. Runs are as long as the workspace: `run_frames` pages.
    merge    Merge `fan_in` runs at a time into one longer run, with one more frame for the page
             being written. A run left over without partners is carried forward untouched, not
             rewritten. Repeat while there are more runs than the last merge can take.
    last     The last merge writes nothing. It hands rows to the parent through next().

The frames (F = frames given to the sort and its input, h = frames the input holds while it
streams, 1 for a table scan):

    pass 0   run_frames = F - h     rows in memory, sorted and written out from those same frames
    merge    fan_in     = F - 1     one frame per input run, one for the output page
    last     final_fan_in runs      one frame per run; normally F - 1, fewer if the parent needs
                                    frames while the sort streams; 1 means a single run on disk

Directly over a table scan the runs are F pages, not F - 1. When the workspace is full, the row
that did not fit and the rest of its page are sitting in the scan's frame. The sort takes that
frame over (the scan unpins its page, the sort reserves the frame, the rows change hands), so
the run is the F - 1 pages of the workspace plus that page. At no moment are more than F frames
held, and this is the textbook pass 0: B pages read, sorted, written.

Only pass 0 calls the language's sort, and only on one workspace of rows. Everything else is the
merge below. Comparisons are counted in both.

If the whole input fits in the workspace and the parent allows the sort to keep those frames
while it streams, nothing is written at all: one pass, zero page writes.
"""
import heapq
from functools import cmp_to_key
from .context import Workspace, RunWriter, RunReader, codec_for
from .operators import Operator, SeqScan, feed, shut


def make_compare(keys, counters):
    """Three-way comparison of two rows on the sort keys. One call is one comparison."""
    def compare(a, b):
        counters.comparisons += 1
        for i, descending in keys:
            x, y = a[i], b[i]
            if x != y:
                return (1 if x < y else -1) if descending else (-1 if x < y else 1)
        return 0
    return compare


def make_entry(compare):
    """Heap entries for the merge: ordered by the sort keys, and by run number among equals so
    that the output order does not depend on anything but the input."""
    class Entry:
        __slots__ = ('row', 'run')

        def __init__(self, row, run):
            self.row = row
            self.run = run

        def __lt__(self, other):
            c = compare(self.row, other.row)
            return c < 0 or (c == 0 and self.run < other.run)
    return Entry


class Sort(Operator):
    name = 'sort'
    min_hold = 1

    def __init__(self, ctx, child, keys, where='', counters=None, on_disk=False):
        """keys is a list of (column index, descending), most significant first.
        counters: an operator that uses a sort inside itself passes its own, so the work is
        reported on that operator. on_disk: always leave the result in run files, never in the
        workspace (needed by a caller that must hold nothing between open and the first row)."""
        super().__init__(ctx, child.columns, [child], where)
        if counters is not None:
            self.c = counters
        self.child = child
        self.keys = keys
        self.on_disk = on_disk
        self.codec = codec_for(child.columns)
        self.compare = make_compare(keys, self.c)
        self.Entry = make_entry(self.compare)
        self.source = None
        self.ws = None
        self.files = set()          # every run file that exists right now
        self.readers = []
        self.runs = []
        self.memory = None          # the sorted rows, when the input fitted in the workspace
        self.reader = None          # the single run being read, when there is exactly one
        self.stream = None          # the last merge, when there are several
        self.initial_runs = 0
        self.passes = 0
        self.run_pages = 0

    def assign(self, frames, limit):
        self.need(frames, limit, 3)
        self.source, held = feed(self, self.child, frames, frames, keep=1)
        self.frames = frames
        self.run_frames = frames - held
        self.fan_in = frames - 1
        self.final_fan_in = max(1, min(limit, frames - 1))
        self.memory_allowed = not self.on_disk and self.run_frames <= limit
        self.hold = max(self.final_fan_in, self.run_frames if self.memory_allowed else 0)

    # ---------------------------------------------------------------- open: pass 0 and the merges

    def open(self):
        self.close()
        source = self.source
        source.open()
        self.ws = Workspace(self.ctx, self.run_frames)
        size = self.codec.size
        scan = isinstance(source, SeqScan)
        self.run_pages = self.run_frames + (1 if scan else 0)
        rows = []
        while True:
            row = source.next()
            if row is None:
                break
            if self.ws.add(size(row)):
                rows.append(row)
            elif scan:                              # full: take over the scan's page and its frame
                rows.append(row)
                rows.extend(source.rest_of_page())
                self.ctx.pool.reserve(1)
                try:
                    self._write_run(rows)
                finally:
                    self.ctx.pool.release(1)
                rows = []
                self.ws.clear()
            else:                                   # full: that is one run; start the next
                self._write_run(rows)
                rows = [row]
                self.ws.clear()
                self.ws.add(size(row))
        shut(source)                                # the input is done; its frames are free
        if rows and not self.runs and self.memory_allowed:
            rows.sort(key=cmp_to_key(self.compare))
            self.memory, self.pos = rows, 0         # keep the workspace: the rows stay in it
        else:
            if rows:
                self._write_run(rows)
            self.ws.close()
            self.ws = None
        self.initial_runs = len(self.runs) + (1 if self.memory is not None else 0)
        self.passes = 1
        while len(self.runs) > self.final_fan_in:
            self._merge_pass()
            self.passes += 1
        if len(self.runs) > 1:
            self.passes += 1                        # the last merge, done while streaming

    def _write_run(self, rows):
        rows.sort(key=cmp_to_key(self.compare))
        writer = RunWriter(self.ctx, self.codec, self.c, own_frame=False)   # the workspace's frames
        for row in rows:
            writer.add(row)
        run = writer.close()
        self.files.add(run)
        self.runs.append(run)

    def _merge_pass(self):
        merged = []
        for start in range(0, len(self.runs), self.fan_in):
            group = self.runs[start:start + self.fan_in]
            if len(group) == 1:
                merged.append(group[0])             # carried forward untouched
                continue
            self.readers = [RunReader(self.ctx, run, self.codec, self.c) for run in group]
            writer = RunWriter(self.ctx, self.codec, self.c)
            try:
                for row in self._merged(self.readers):
                    writer.add(row)
            finally:
                out = writer.close()
                if out is not None:
                    self.files.add(out)
                self._close_readers()
            for run in group:
                self.files.discard(run)
                run.delete()
            merged.append(out)
        self.runs = merged

    def _merged(self, readers):
        """The rows of several sorted runs as one sorted stream."""
        Entry = self.Entry
        heap = []
        for number, reader in enumerate(readers):
            row = reader.next()
            if row is not None:
                heap.append(Entry(row, number))
        heapq.heapify(heap)
        while heap:
            smallest = heap[0]
            yield smallest.row
            row = readers[smallest.run].next()
            if row is None:
                heapq.heappop(heap)
            else:
                heapq.heapreplace(heap, Entry(row, smallest.run))

    # ---------------------------------------------------------------- next: the last pass

    def _next(self):
        if self.memory is not None:
            if self.pos >= len(self.memory):
                return None
            self.pos += 1
            return self.memory[self.pos - 1]
        if self.reader is None and self.stream is None:
            if not self.runs:
                return None
            self.readers = [RunReader(self.ctx, run, self.codec, self.c) for run in self.runs]
            if len(self.readers) == 1:
                self.reader = self.readers[0]
            else:
                self.stream = self._merged(self.readers)
        if self.reader is not None:
            return self.reader.next()
        return next(self.stream, None)

    def mark(self):
        """The position of the row most recently returned. Only when the result is one run."""
        return self.pos - 1 if self.memory is not None else self.reader.mark()

    def seek(self, mark):
        """Go back: the next row returned is the marked one."""
        if self.memory is not None:
            self.pos = mark
        else:
            self.reader.seek(mark)

    # ---------------------------------------------------------------- close

    def _close_readers(self):
        for reader in self.readers:
            reader.close()
        self.readers = []

    def close(self):
        self._close_readers()
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        for run in self.files:
            run.delete()
        self.files = set()
        self.runs = []
        self.memory = self.reader = self.stream = None
        shut(self.source)

    def details(self):
        return {'run_pages': self.run_pages, 'fan_in': self.fan_in, 'final_fan_in': self.final_fan_in,
                'initial_runs': self.initial_runs, 'passes': self.passes}
