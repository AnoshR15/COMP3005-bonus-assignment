"""The three join algorithms. The output schema of each is the left schema followed by the right.

    nested_loop_join   block nested loop; any condition
    sort_merge_join    equality only; sorts both inputs, merges with mark and rewind
    hash_join          equality only; Grace hash join; left is the build side
"""
from collections import deque
from source.ast import Attr, Compare, Logic
from source.operators import resolve_attr
from .context import PlanError, Workspace, RunReader, codec_for, partition, hash_key
from .operators import (Operator, SeqScan, Rename, Spool, feed, shut, check_unique, compile_condition,
                        engine_schema)
from .sort import Sort

PROBE_SEED = 1000           # h2: the hash function of the in-memory table, unlike any partition seed


def equi_keys(left, right, condition, where, name):
    """The key columns of an equality join: ([left indexes], [right indexes]).
    The condition must be  l = r  or several of them joined by and."""
    nl = len(left.columns)
    columns = left.columns + right.columns
    tree, _ = compile_condition(columns, condition, where)      # names and types checked here
    schema = engine_schema(columns)
    lkeys, rkeys = [], []

    def walk(node):
        if isinstance(node, Logic) and node.op == 'and':
            walk(node.left)
            walk(node.right)
            return
        if isinstance(node, Compare) and node.op == '=' and isinstance(node.left, Attr) \
                and isinstance(node.right, Attr):
            i, j = resolve_attr(schema, node.left), resolve_attr(schema, node.right)
            if i > j:
                i, j = j, i
            if i < nl <= j:
                lkeys.append(i)
                rkeys.append(j - nl)
                return
        raise PlanError(f"{where}: field 'condition': {name} needs equalities between a left column "
                        f"and a right column, joined by 'and'; got \"{condition}\"")

    walk(tree)
    return lkeys, rkeys


def key_function(indexes):
    if len(indexes) == 1:
        i = indexes[0]
        return lambda row: (row[i],)
    return lambda row: tuple([row[i] for i in indexes])


class Join(Operator):

    def __init__(self, ctx, left, right, where):
        columns = left.columns + right.columns
        check_unique(columns, where)
        super().__init__(ctx, columns, [left, right], where)
        self.left, self.right = left, right
        self.left_codec = codec_for(left.columns)
        self.right_codec = codec_for(right.columns)
        self.out = None

    def _next(self):
        return next(self.out, None)


# ------------------------------------------------------------------ nested loop

class NestedLoopJoin(Join):
    """Block nested loop.

    Fill the block with outer rows, then run the whole inner input past it, testing every pair;
    repeat for the next block. Every pair is compared exactly once, so comparisons = |R| * |S|
    whatever the condition, the matches or the frames.

    Frames while streaming, `hold` in all:   outer input's own + block_frames + 1 for the inner.
    With two table scans that is 1 + (hold - 2) + 1, the textbook B - 2 pages of outer per block.
    There is no output frame: a matching pair goes straight to the parent.

    The inner input is read once per block. A table scan, renamed or not, is simply opened
    again. Anything else (a filter, a projection, a join) is spooled to a temporary file once,
    and that file is read again for every block.

    With no condition every pair is output: a cross product."""
    name = 'nested_loop_join'
    min_hold = 3

    def __init__(self, ctx, left, right, condition, where):
        super().__init__(ctx, left, right, where)
        self.test = None
        if condition is not None:
            _, self.test = compile_condition(self.columns, condition, where)
        self.outer = self.inner = self.ws = None
        self.blocks = 0

    @staticmethod
    def rescannable(op):
        """A table scan, possibly under renames: opening it again costs nothing but the reads."""
        while isinstance(op, Rename):
            op = op.child
        return isinstance(op, SeqScan)

    def assign(self, frames, limit):
        self.need(frames, limit, 3, 3)
        total = min(frames, limit)
        if self.rescannable(self.right):
            self.right.assign(1, 1)
            self.inner = self.right
        else:
            self.inner = Spool(self.ctx, self.right, self.c)
            self.inner.assign(frames, 1)
        self.outer, held = feed(self, self.left, frames, total, keep=2)
        self.frames, self.hold = frames, total
        self.block_frames = total - held - 1

    def open(self):
        self.close()
        self.blocks = 0
        if isinstance(self.inner, Spool):
            self.inner.materialize()        # before anything else holds a frame
        self.outer.open()
        self.ws = Workspace(self.ctx, self.block_frames)
        self.out = self._pairs()

    def _pairs(self):
        outer, inner, ws, test, c = self.outer, self.inner, self.ws, self.test, self.c
        size = self.left_codec.size
        waiting = outer.next()              # the first outer row not yet in a block
        while waiting is not None:
            block = []
            ws.clear()
            while waiting is not None and ws.add(size(waiting)):
                block.append(waiting)
                waiting = outer.next()
            self.blocks += 1
            inner.open()
            try:
                while True:
                    s = inner.next()
                    if s is None:
                        break
                    for r in block:
                        c.comparisons += 1
                        pair = r + s
                        if test is None or test(pair):
                            yield pair
            finally:
                inner.close()

    def close(self):
        if self.out is not None:
            self.out.close()
            self.out = None
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        shut(self.inner)
        shut(self.outer)

    def details(self):
        spooled = isinstance(self.inner, Spool)
        scan = self.right
        while isinstance(scan, Rename):
            scan = scan.child
        return {'block_frames': self.block_frames, 'blocks': self.blocks, 'inner_spooled': spooled,
                'inner_pages': self.inner.pages if spooled else scan.info.num_pages}


# ------------------------------------------------------------------ sort merge

class SortMergeJoin(Join):
    """Sort both inputs on the join key, each into a single run on disk, then walk the two runs
    in step with one frame each.

    Equal keys on both sides: when the keys match, the position of the first matching right row
    is marked. For each left row of that key the right side is rewound to the mark and its run of
    equal keys is read again, so every left row of the key meets every right row of the key.
    The right run is never held in memory. It is already in the sorted file, so rewinding is a
    seek, and a run of any length works: it costs page reads, one per page each time the reader
    has to go back to an earlier page, not frames.

    comparisons = key comparisons of the two sorts + key comparisons of the merge."""
    name = 'sort_merge_join'
    min_hold = 2

    def __init__(self, ctx, left, right, condition, where):
        super().__init__(ctx, left, right, where)
        lkeys, rkeys = equi_keys(left, right, condition, where, self.name)
        self.left_key, self.right_key = key_function(lkeys), key_function(rkeys)
        self.left_sort = Sort(ctx, left, [(i, False) for i in lkeys], where, self.c, on_disk=True)
        self.right_sort = Sort(ctx, right, [(i, False) for i in rkeys], where, self.c, on_disk=True)

    def assign(self, frames, limit):
        self.need(frames, limit, 3, 2)
        self.left_sort.assign(frames, 1)        # 1: merge all the way down to one run
        self.right_sort.assign(frames, 1)
        self.frames, self.hold = frames, 2

    def open(self):
        self.close()
        self.left_sort.open()                   # holds nothing afterwards: its run is on disk
        self.right_sort.open()
        self.out = self._merge()

    def _merge(self):
        left, right, c = self.left_sort, self.right_sort, self.c
        left_key, right_key = self.left_key, self.right_key
        r, s = left._next(), right._next()
        while r is not None and s is not None:
            key, other = left_key(r), right_key(s)
            c.comparisons += 1
            if key < other:
                r = left._next()
            elif key > other:
                s = right._next()
            else:
                mark = right.mark()             # the first right row with this key
                while True:
                    right.seek(mark)
                    s = right._next()
                    while s is not None:
                        c.comparisons += 1
                        if right_key(s) != key:
                            break
                        yield r + s
                        s = right._next()
                    r = left._next()            # the next left row: same key again?
                    if r is None:
                        break
                    c.comparisons += 1
                    if left_key(r) != key:
                        break
                # here r and s are the first rows after their runs of this key, or None

    def close(self):
        if self.out is not None:
            self.out.close()
            self.out = None
        self.left_sort.close()
        self.right_sort.close()

    def details(self):
        return {'left_sort': self.left_sort.details(), 'right_sort': self.right_sort.details()}


# ------------------------------------------------------------------ hash

class HashJoin(Join):
    """Grace hash join.

    Partition phase, inside open(). The left input is read once and each row is written to
    partition h(key) mod P; then the same for the right input. Rows with equal keys are now in
    partitions with the same number. P = frames - (frames the input holds), B - 1 over a scan.

    Probe phase, one pair of partitions at a time. Read the left partition into an in-memory
    table of hold - 1 frames, using a second hash function; then read the right partition
    through the remaining frame and look each row up.

    Overflow. A left partition that does not fit in hold - 1 frames is not loaded. The pair is
    partitioned again with the next hash function, into hold - 1 parts, up to MAX_LEVEL levels of
    partitioning in all. A pair that is still too big after that is joined by block nested loop,
    which needs no table: no hash function can split rows that all have the same key.

    comparisons: one for every left row whose key is compared with a right row's key, in a
    bucket of the table or in the nested loop. Partitioning compares nothing."""
    name = 'hash_join'
    min_hold = 3
    MAX_LEVEL = 3

    def __init__(self, ctx, left, right, condition, where):
        super().__init__(ctx, left, right, where)
        lkeys, rkeys = equi_keys(left, right, condition, where, self.name)
        self.left_key, self.right_key = key_function(lkeys), key_function(rkeys)
        self.left_source = self.right_source = self.ws = None
        self.files = set()
        self.todo = deque()
        self.info = {}

    def assign(self, frames, limit):
        self.need(frames, limit, 3, 3)
        self.left_source, left_held = feed(self, self.left, frames, frames, keep=2)
        self.right_source, right_held = feed(self, self.right, frames, frames, keep=2)
        self.frames, self.hold = frames, min(frames, limit)
        self.partitions = frames - max(left_held, right_held)

    def open(self):
        self.close()
        left = self._partition_input(self.left_source, self.left_codec, self.left_key)
        right = self._partition_input(self.right_source, self.right_codec, self.right_key)
        self.info = {'partitions': self.partitions, 'table_frames': self.hold - 1,
                     'left_partition_pages': [f.num_pages if f else 0 for f in left],
                     'right_partition_pages': [f.num_pages if f else 0 for f in right],
                     'overflows': []}
        self._queue(left, right, 1)
        self.out = self._run()

    def _partition_input(self, source, codec, key):
        source.open()
        try:
            parts = partition(self.ctx, source.next, codec, key, self.partitions, 0, self.c)
        finally:
            shut(source)
        self.files.update(p for p in parts if p is not None)
        return parts

    def _queue(self, left, right, level):
        """Line up pairs of partitions for joining, next in line. A pair with an empty side can
        produce nothing, so it is dropped."""
        pairs = []
        for l, r in zip(left, right):
            if l is not None and r is not None:
                pairs.append((l, r, level))
            else:
                self._drop(l, r)
        self.todo.extendleft(reversed(pairs))

    def _drop(self, *files):
        for f in files:
            if f is not None:
                self.files.discard(f)
                f.delete()

    def _run(self):
        while self.todo:
            yield from self._join_pair(*self.todo.popleft())

    def _join_pair(self, left, right, level):
        # Build: the left partition goes into memory, if it fits in hold - 1 frames.
        self.ws = Workspace(self.ctx, self.hold - 1)
        reader = RunReader(self.ctx, left, self.left_codec, self.c)
        size, rows, fits = self.left_codec.size, [], True
        try:
            while True:
                row = reader.next()
                if row is None:
                    break
                if not self.ws.add(size(row)):
                    fits = False
                    break
                rows.append(row)
        finally:
            reader.close()
        if not fits:
            self.ws.close()
            self.ws = None
            again = level < self.MAX_LEVEL
            self.info['overflows'].append({'level': level, 'left_pages': left.num_pages,
                                           'right_pages': right.num_pages,
                                           'action': 'partitioned again' if again else 'nested loop'})
            if again:
                self._queue(self._split(left, self.left_codec, self.left_key, level),
                            self._split(right, self.right_codec, self.right_key, level), level + 1)
            else:
                yield from self._nested_loop(left, right)
            self._drop(left, right)
            return
        buckets = [[] for _ in rows]
        count = len(buckets)
        left_key, right_key, c = self.left_key, self.right_key, self.c
        for row in rows:
            key = left_key(row)
            buckets[hash_key(key, PROBE_SEED) % count].append((key, row))
        rows = None
        # Probe: the right partition streams past the table through one frame.
        reader = RunReader(self.ctx, right, self.right_codec, self.c)
        try:
            while True:
                s = reader.next()
                if s is None:
                    break
                key = right_key(s)
                for candidate, r in buckets[hash_key(key, PROBE_SEED) % count]:
                    c.comparisons += 1
                    if candidate == key:
                        yield r + s
        finally:
            reader.close()
            self.ws.close()
            self.ws = None
            self._drop(left, right)

    def _split(self, file, codec, key, level):
        """Partition one partition again, with the hash function of the next level."""
        reader = RunReader(self.ctx, file, codec, self.c)
        try:
            parts = partition(self.ctx, reader.next, codec, key, self.hold - 1, level, self.c)
        finally:
            reader.close()
        self.files.update(p for p in parts if p is not None)
        return parts

    def _nested_loop(self, left, right):
        """Join one pair of partitions without a table: blocks of left rows, the right file read
        once per block. Frames: hold - 2 for the block, one for each file."""
        self.ws = Workspace(self.ctx, self.hold - 2)
        left_key, right_key, c = self.left_key, self.right_key, self.c
        size = self.left_codec.size
        left_reader = RunReader(self.ctx, left, self.left_codec, self.c)
        right_reader = None
        try:
            waiting = left_reader.next()
            while waiting is not None:
                block = []
                self.ws.clear()
                while waiting is not None and self.ws.add(size(waiting)):
                    block.append((left_key(waiting), waiting))
                    waiting = left_reader.next()
                right_reader = RunReader(self.ctx, right, self.right_codec, self.c)
                while True:
                    s = right_reader.next()
                    if s is None:
                        break
                    key = right_key(s)
                    for candidate, r in block:
                        c.comparisons += 1
                        if candidate == key:
                            yield r + s
                right_reader.close()
                right_reader = None
        finally:
            left_reader.close()
            if right_reader is not None:
                right_reader.close()
            self.ws.close()
            self.ws = None

    def close(self):
        if self.out is not None:
            self.out.close()
            self.out = None
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        self._drop(*list(self.files))
        self.todo.clear()
        shut(self.left_source)
        shut(self.right_source)

    def details(self):
        return dict(self.info)
