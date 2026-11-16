"""The iterator interface, the rule that divides the frames, and the pipelined operators.

Every operator has

    open()      prepare; a blocking operator does its blocking work here
    next()      the next output row, or None at the end of the stream
    close()     release every frame and delete every temporary file; safe to call twice
    counters()  {tuples_out, comparisons, page_reads, page_writes} of this operator alone

HOW THE FRAMES ARE DIVIDED

Before a plan runs, assign(frames, limit) is called once on the root with (B, B) and walks down:

    frames   the most the operator and everything below it may hold at any moment
    limit    the most they may hold while the operator is streaming rows to its parent,
             that is between the end of open() and close()

and each operator sets `hold`, the number it will actually hold while streaming (hold <= limit).
The two numbers differ because of time. A blocking child finishes its heavy work inside its own
open(), before its parent has reserved anything, so it may use all of `frames` then. Afterwards
it only streams, and whatever it still holds comes out of what the parent has to work with.

Three rules decide everything:

  1. A scan holds one frame, its current page. filter, project, rename, limit and union_all hold
     none of their own: the row passing through belongs to the frame of whoever produced it.
  2. An operator that needs working memory gets all the frames its inputs are not holding while
     they stream to it.
  3. When an input itself needs working memory while it streams (a join that is still probing,
     a sort still merging), the two share: the input may hold half, rounded down. If its minimum
     is more than half it gets exactly its minimum, provided the parent can still work in the
     rest. If even that is impossible, the input's output is spooled: written to a temporary
     file first, with the whole allowance to itself, and read back through one frame.

feed() below is rules 2 and 3.
"""
from collections import namedtuple
from source.condition import parse_condition
from source.errors import RAError
from source.operators import SchemaAttr, NUMBER, STRING, bind, resolve_attr
from source.parser import Parser
from source.tokenizer import Tokenizer
from store import page
from store.heap import is_directory_page
from store.record import INT, VARCHAR, REAL, WIDE
from .context import Counters, ExecError, BudgetError, PlanError, RunWriter, RunReader, codec_for

Column = namedtuple('Column', 'name kind size')         # name is qualified, such as R.a


def type_text(column):
    return f'VARCHAR({column.size})' if column.kind == VARCHAR else column.kind


def engine_schema(columns):
    """The same schema in the form the Bonus Project 1 condition code understands."""
    out = []
    for c in columns:
        table, dot, name = c.name.partition('.')
        out.append(SchemaAttr(table if dot else None, name if dot else table,
                              STRING if c.kind == VARCHAR else NUMBER))
    return out


def column_index(columns, text, where):
    """Position of the column a plan names, such as "R.a"."""
    try:
        parser = Parser(Tokenizer(text).scan())
        attr = parser.attr()
        parser.expect('EOF', what='end of column name')
        return resolve_attr(engine_schema(columns), attr)
    except RAError as e:
        raise PlanError(f'{where}: {lower_first(str(e))}') from None


def compile_condition(columns, text, where):
    """Condition string -> (tree, function row -> bool), checked against the schema."""
    try:
        tree = parse_condition(text)
        return tree, bind(tree, engine_schema(columns))
    except RAError as e:
        raise PlanError(f"{where}: field 'condition': {lower_first(str(e))}") from None


def lower_first(text):
    return text[:1].lower() + text[1:]


def check_unique(columns, where):
    seen = set()
    for c in columns:
        if c.name in seen:
            raise PlanError(f"{where}: the output would have two columns named '{c.name}'")
        seen.add(c.name)


# ------------------------------------------------------------------ the base class

class Operator:
    name = 'operator'
    min_hold = 1            # the fewest frames it can hold while streaming

    def __init__(self, ctx, columns, children=(), where=''):
        self.ctx = ctx
        self.columns = list(columns)
        self.children = list(children)      # the plan's children, for trace output
        self.where = where
        self.c = Counters()
        self.hold = None
        self.frames = None
        self.label = self.name

    def assign(self, frames, limit):
        raise NotImplementedError

    def open(self):
        pass

    def next(self):
        row = self._next()
        if row is not None:
            self.c.tuples_out += 1
        return row

    def _next(self):
        raise NotImplementedError

    def close(self):
        pass

    def counters(self):
        return self.c.as_dict()

    def details(self):
        """Extra facts for reports: passes, runs, partition sizes. Not part of the counters."""
        return {}

    def need(self, frames, limit, least_frames, least_limit=1):
        if frames < least_frames or limit < least_limit:
            raise BudgetError(f'{self.name} needs {least_frames} frames'
                              + (f' and {least_limit} while streaming' if least_limit > 1 else '')
                              + f'; it was given {frames} and {limit}')


def feed(owner, child, frames, total, keep):
    """Decide how `child` delivers its rows to `owner`, and return (source, frames it holds).

    frames  what the child's subtree may use while it opens
    total   what owner and child may hold together while the child streams
    keep    what owner needs for itself during that time
    """
    half = total // 2
    if child.min_hold <= half and total - half >= keep:
        limit = half
    elif child.min_hold <= total - keep:
        limit = child.min_hold
    else:
        spool = Spool(owner.ctx, child, owner.c)
        spool.assign(frames, 1)
        return spool, 1
    child.assign(frames, limit)
    return child, child.hold


def least_frames(op):
    """The fewest frames an operator can run in at all, found by trying 1, 2, 3, ..."""
    for frames in range(1, 65):
        try:
            op.assign(frames, frames)
            return frames
        except BudgetError:
            pass
    raise BudgetError(f'{op.name} cannot run in 64 frames')


def shut(source):
    """Close an input, and delete its file if it was spooled."""
    if source is not None:
        source.close()
        if isinstance(source, Spool):
            source.discard()


class Spool:
    """Writes a child's whole output to a temporary file, then replays it through one frame.

    Not a plan node: it is put in by feed() when the frames do not allow two operators to work
    at the same time, and by a nested loop join around an inner input that is not a plain scan.
    Its page writes and reads are charged to the operator that needed it."""
    min_hold = 1

    def __init__(self, ctx, child, counters):
        self.ctx = ctx
        self.child = child
        self.columns = child.columns
        self.c = counters
        self.codec = codec_for(child.columns)
        self.file = None
        self.done = False
        self.reader = None
        self.hold = 1
        self.pages = 0

    def assign(self, frames, limit):
        if limit < 1 or frames < self.child.min_hold + 1:
            raise BudgetError(f'spooling {self.child.name} needs {self.child.min_hold + 1} frames; '
                              f'it was given {frames}')
        self.child.assign(frames, frames - 1)           # one frame stays free for the spool's page

    def materialize(self):
        if not self.done:
            self.child.open()
            writer = RunWriter(self.ctx, self.codec, self.c)
            try:
                while True:
                    row = self.child.next()
                    if row is None:
                        break
                    writer.add(row)
            finally:
                self.file = writer.close()
                self.child.close()
            self.done = True
            self.pages = self.file.num_pages if self.file else 0

    def open(self):
        self.materialize()
        self.close()
        if self.file is not None:
            self.reader = RunReader(self.ctx, self.file, self.codec, self.c)

    def next(self):
        return self.reader.next() if self.reader is not None else None

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def discard(self):
        self.close()
        self.child.close()
        if self.file is not None:
            self.file.delete()
            self.file = None
        self.done = False


class FileScan:
    """Reads a temporary file as an input. Internal: used when an operator has to run another
    operator (a sort) over one of its own files. The file stays the property of that operator."""
    min_hold = 1
    name = 'file'

    def __init__(self, ctx, file, columns, counters):
        self.ctx = ctx
        self.file = file
        self.columns = columns
        self.c = counters
        self.codec = codec_for(columns)
        self.reader = None
        self.hold = 1

    def assign(self, frames, limit):
        if frames < 1 or limit < 1:
            raise BudgetError('reading a temporary file needs one frame')

    def open(self):
        self.reader = RunReader(self.ctx, self.file, self.codec, self.c)

    def next(self):
        return self.reader.next()

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None


# ------------------------------------------------------------------ seqscan

class SeqScan(Operator):
    """Every live row of a table in heap order. One data page is pinned at a time; the pinned
    page is the scan's one frame. Reads exactly as many pages as the table has data pages.

    The directory pages are not read. A scan that follows the directory (HeapFile.scan, used by
    the storage tool) keeps a directory page pinned next to the data page: two frames. Here
    there is one. A table file is always directory, 681 data pages, directory, 681 data pages,
    and so on, because pages are only added at the end; so the data pages in directory order are
    simply the page ids that are not multiples of 682, and the scan walks those."""
    name = 'seqscan'

    def __init__(self, ctx, table, where):
        info = ctx.catalog.get_table(table)
        if info is None:
            raise PlanError(f"{where}: unknown table '{table}'")
        super().__init__(ctx, [Column(f'{table}.{n}', kind, size) for n, kind, size in info.columns], (), where)
        self.info = info
        self.codec = info.codec()
        self.label = f'seqscan {table}'
        self.file = info.file
        self.pinned = None
        self.rows = []
        self.i = 0
        self.next_page = self.end = 0

    def assign(self, frames, limit):
        self.need(frames, limit, 1)
        self.frames, self.hold = frames, 1

    def open(self):
        self.close()
        self.next_page = 1
        self.end = self.ctx.disk.num_pages(self.file)

    def _next(self):
        while self.i >= len(self.rows):
            self._unpin()
            if is_directory_page(self.next_page):
                self.next_page += 1
            if self.next_page >= self.end:
                self.rows, self.i = [], 0
                return None
            frame = self.ctx.pool.fetch_page(self.file, self.next_page, self.c)
            self.pinned = self.next_page
            decode = self.codec.decode
            self.rows = [decode(record) for _, record in page.records(frame.data)]
            self.i = 0
            self.next_page += 1
        row = self.rows[self.i]
        self.i += 1
        return row

    def rest_of_page(self):
        """Hand over the rows of the current page that have not been returned yet, and the frame
        with them: the page is unpinned, and the scan holds nothing until it is asked for its
        next row. The caller must have a frame of its own to keep these rows in."""
        rest = self.rows[self.i:]
        self._unpin()
        self.rows, self.i = [], 0
        self.c.tuples_out += len(rest)
        return rest

    def _unpin(self):
        if self.pinned is not None:
            self.ctx.pool.unpin_page(self.file, self.pinned)
            self.pinned = None

    def close(self):
        self._unpin()
        self.rows, self.i = [], 0
        self.next_page = self.end = 0


# ------------------------------------------------------------------ pipelined, one input

class Pipelined(Operator):
    """One input, no memory of its own: it holds whatever its child holds."""

    def __init__(self, ctx, child, columns, where):
        super().__init__(ctx, columns, [child], where)
        self.child = child
        self.min_hold = child.min_hold

    def assign(self, frames, limit):
        self.child.assign(frames, limit)
        self.frames, self.hold = frames, self.child.hold

    def open(self):
        self.child.open()

    def close(self):
        self.child.close()


class Filter(Pipelined):
    """Passes on the rows for which the condition holds. One comparison is counted per row
    examined, however many comparison operators the condition contains."""
    name = 'filter'

    def __init__(self, ctx, child, condition, where):
        super().__init__(ctx, child, child.columns, where)
        _, self.test = compile_condition(child.columns, condition, where)

    def _next(self):
        child, test, c = self.child, self.test, self.c
        while True:
            row = child.next()
            if row is None:
                return None
            c.comparisons += 1
            if test(row):
                return row


class Project(Pipelined):
    """The listed columns in the listed order. Duplicates are kept. A column written as
    ["R.a", "x"] is R.a under the new name x."""
    name = 'project'

    def __init__(self, ctx, child, columns, where):
        self.indexes = []
        out = []
        for item in columns:
            source, alias = item if isinstance(item, list) else (item, None)
            i = column_index(child.columns, source, where)
            self.indexes.append(i)
            c = child.columns[i]
            out.append(Column(alias, c.kind, c.size) if alias else c)
        check_unique(out, where)
        super().__init__(ctx, child, out, where)

    def _next(self):
        row = self.child.next()
        if row is None:
            return None
        return tuple([row[i] for i in self.indexes])


class Rename(Pipelined):
    """Gives every column of its input the qualifier `to`: Emp.name becomes e.name."""
    name = 'rename'

    def __init__(self, ctx, child, to, where):
        out = [Column(f"{to}.{c.name.rpartition('.')[2]}", c.kind, c.size) for c in child.columns]
        check_unique(out, where)
        super().__init__(ctx, child, out, where)
        self.label = f'rename {to}'

    def _next(self):
        return self.child.next()


class Limit(Pipelined):
    """The first `count` rows. After the last of them it does not call its child again."""
    name = 'limit'

    def __init__(self, ctx, child, count, where):
        super().__init__(ctx, child, child.columns, where)
        self.count = count
        self.sent = 0
        self.label = f'limit {count}'

    def open(self):
        self.sent = 0
        self.child.open()

    def _next(self):
        if self.sent >= self.count:
            return None
        row = self.child.next()
        if row is not None:
            self.sent += 1
        return row


# ------------------------------------------------------------------ union_all

def check_same_types(left, right, where):
    if len(left) != len(right):
        raise PlanError(f'{where}: the left input has {len(left)} columns and the right has {len(right)}')
    for i, (a, b) in enumerate(zip(left, right), start=1):
        if (a.kind == VARCHAR) != (b.kind == VARCHAR):
            raise PlanError(f'{where}: column {i} is {a.kind} on the left ({a.name}) '
                            f'and {b.kind} on the right ({b.name})')


def merged_columns(left, right):
    """Output columns of a set operator: the left names, wide enough for either side's values:
    the longer VARCHAR, and an 8-byte INT if either side is one (a SUM)."""
    out = []
    for a, b in zip(left, right):
        if a.kind == VARCHAR:
            size = max(a.size, b.size)
        else:
            size = WIDE if a.kind == b.kind == INT and WIDE in (a.size, b.size) else None
        out.append(Column(a.name, a.kind if a.kind == b.kind else REAL, size))
    return out


class UnionAll(Operator):
    """All rows of the left input, then all rows of the right. The right input is opened only
    after the left one is closed, so the two never hold frames at the same time.

    That late opening has a price. Every other operator opens its inputs before its parent has
    reserved anything, so their blocking work has the whole allowance. The right input here is
    opened in the middle of the stream, when the operators above may already hold everything
    but `limit` frames. So it must do all of its work in `limit` frames. If it cannot, it is
    spooled instead: run to completion inside open(), while the whole allowance is still free,
    and replayed through one frame when its turn comes."""
    name = 'union_all'

    def __init__(self, ctx, left, right, where):
        check_same_types(left.columns, right.columns, where)
        super().__init__(ctx, merged_columns(left.columns, right.columns), [left, right], where)
        self.left, self.right = left, right
        self.min_hold = left.min_hold
        self.right_needs = least_frames(right)
        self.second = right                 # the right input, or the spool in front of it
        self.current = None

    def assign(self, frames, limit):
        self.need(frames, limit, self.min_hold, self.min_hold)
        self.left.assign(frames, limit)
        if limit >= self.right_needs:
            self.right.assign(limit, limit)
            self.second = self.right
            self.hold = limit               # its open() may use all of them, in mid-stream
        else:
            self.second = Spool(self.ctx, self.right, self.c)
            self.second.assign(frames, 1)
            self.hold = self.left.hold
        self.frames = frames

    def open(self):
        self.close()
        if isinstance(self.second, Spool):
            self.second.materialize()       # before anything holds a frame
        self.left.open()
        self.current = self.left

    def _next(self):
        while self.current is not None:
            row = self.current.next()
            if row is not None:
                return row
            self.current.close()
            if self.current is self.left:
                self.second.open()
                self.current = self.second
            else:
                self.current = None
        return None

    def close(self):
        self.left.close()
        shut(self.second)
        self.current = None

    def details(self):
        return {'right_spooled': isinstance(self.second, Spool)}
