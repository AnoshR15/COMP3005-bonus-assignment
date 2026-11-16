"""group_by and distinct, each in a sort-based and a hash-based version.

distinct is group_by on every column with no aggregates, so there is one implementation.

Sort mode. Sort the input on the grouping columns with the external sort, then walk the sorted
rows: a group ends where the key changes. It costs what the sort costs.

Hash mode. Keep a table of groups (key -> running aggregates) in the workspace.
    - While a new group still fits, add it. A row whose group is already in the table is
      absorbed into it and never needs storing.
    - When the table is full it is frozen: rows of groups it has keep updating them, rows of
      any other group are written to a spill file.
    - At the end of the input the groups in the table are complete and are output.
    - The spill file is then split into partitions by a hash of the key, and each partition is
      grouped the same way, with a new table. A partition that overflows spills again and is
      split with a different hash function. After MAX_LEVEL rounds a spill file is grouped by
      sorting it instead, which works for any size.
If the table never fills, hash mode does no I/O of its own at all.

With no grouping columns there is one group: no sort, no table, one output row, and that row
exists even when the input is empty.
"""
from store.record import INT, VARCHAR, REAL, WIDE
from .context import ExecError, PlanError, Workspace, RunWriter, RunReader, codec_for, partition
from .operators import Operator, Column, FileScan, feed, shut, column_index
from .sort import Sort

MAX_LEVEL = 3
AGGREGATES = ('count', 'sum', 'avg', 'min', 'max')


def aggregate_name(func, column):
    """count_star, avg_R_salary, max_R_name."""
    return func + '_' + ('star' if column is None else column.replace('.', '_'))


class Aggregates:
    """The running state of a list of aggregates, one group at a time."""

    def __init__(self, specs, columns, where):
        """specs: list of (function, column index or None for *)."""
        self.specs = specs
        self.names = [f"{f}({'*' if i is None else columns[i].name})" for f, i in specs]
        self.where = where
        # Bytes one group's state takes, for the workspace: a counter, a sum, a sum and a
        # counter, or the widest value the column can hold.
        self.state_size = 0
        for func, i in specs:
            if func == 'count':
                self.state_size += 4
            elif func in ('sum', 'avg'):
                self.state_size += 8 if func == 'sum' else 12
            else:
                c = columns[i]
                self.state_size += 1 + c.size if c.kind == VARCHAR else 8

    def start(self):
        return [0 if func == 'count' else [0, 0] if func == 'avg' else None for func, _ in self.specs]

    def update(self, state, row):
        for j, (func, i) in enumerate(self.specs):
            if func == 'count':
                state[j] += 1
                continue
            value = row[i]
            if func == 'avg':
                state[j][0] += value
                state[j][1] += 1
            elif state[j] is None:
                state[j] = value
            elif func == 'sum':
                state[j] += value
            elif func == 'min':
                if value < state[j]:
                    state[j] = value
            elif value > state[j]:
                state[j] = value

    def finish(self, state):
        out = []
        for j, (func, _) in enumerate(self.specs):
            value = state[j]
            if func == 'avg':
                if value[1] == 0:
                    raise ExecError(f'{self.where}: {self.names[j]} of an empty input is undefined')
                value = value[0] / value[1]
            elif value is None:
                raise ExecError(f'{self.where}: {self.names[j]} of an empty input is undefined')
            out.append(value)
        return tuple(out)


class GroupBy(Operator):
    name = 'group_by'

    def __init__(self, ctx, child, mode, keys, aggregates, where, counters=None):
        """keys: column names (or positions). aggregates: [function, column or "*"] with an
        optional third element, the output name."""
        self.key_idx = [k if isinstance(k, int) else column_index(child.columns, k, where) for k in keys]
        out = [child.columns[i] for i in self.key_idx]
        specs = []
        for item in aggregates:
            func, arg = item[0], item[1]
            if arg == '*':
                if func != 'count':
                    raise PlanError(f'{where}: {func}(*) is not defined; only count accepts *')
                i, kind, size, source = None, INT, None, None
            else:
                i = column_index(child.columns, arg, where)
                column = child.columns[i]
                source = column.name
                if func in ('sum', 'avg') and column.kind == VARCHAR:
                    raise PlanError(f'{where}: {func}({column.name}) needs an INT column; it is VARCHAR')
                kind = INT if func == 'count' else REAL if func == 'avg' else column.kind
                # A sum of 4-byte values can pass 2,147,483,647, so in a temporary file it gets
                # 8 bytes. It is still an INT to everything that asks about types.
                size = WIDE if func == 'sum' else column.size if func in ('min', 'max') else None
            specs.append((func, i))
            out.append(Column(item[2] if len(item) == 3 else aggregate_name(func, source), kind, size))
        names = [c.name for c in out]
        for n in names:
            if names.count(n) > 1:
                raise PlanError(f"{where}: the output would have two columns named '{n}'")
        super().__init__(ctx, out, [child], where)
        if counters is not None:
            self.c = counters
        self.child = child
        self.mode = mode
        self.aggs = Aggregates(specs, child.columns, where)
        self.in_codec = codec_for(child.columns)
        self.key_codec = codec_for([child.columns[i] for i in self.key_idx])
        self.grouped = bool(self.key_idx)
        self.by_sort = self.grouped and mode == 'sort'
        self.sorter = None
        if self.by_sort:
            self.sorter = Sort(ctx, child, [(i, False) for i in self.key_idx], where, counters=self.c)
        self.min_hold = 3 if self.grouped and not self.by_sort else 1 if self.by_sort else child.min_hold
        self.source = None
        self.table_ws = None
        self.out = None
        self.pending = []           # ('spill' or 'part', file, level) still to be grouped
        self.fallback = None        # (sort, file) while a spill file is being grouped by sorting
        self.spilled_rows = self.partitions_made = self.sort_fallbacks = 0

    def key_of(self, row):
        return tuple([row[i] for i in self.key_idx])

    # ---------------------------------------------------------------- frames

    def assign(self, frames, limit):
        self.frames = frames
        if not self.grouped:                # one group: a single row of running totals, no frames
            self.child.assign(frames, limit)
            self.source, self.hold = self.child, self.child.hold
        elif self.by_sort:
            self.sorter.assign(frames, limit)
            self.hold = self.sorter.hold
        else:
            # While the input streams: its frames, the table, one page of the spill file.
            # Afterwards, `hold` frames: one page being read, the table, one page of spill file;
            # or one page being read and hold - 1 partitions being written.
            self.need(frames, limit, 3, 3)
            self.source, held = feed(self, self.child, frames, frames, keep=2)
            self.hold = min(frames, limit)
            self.first_table = min(frames - held - 1, self.hold)

    # ---------------------------------------------------------------- open, next, close

    def open(self):
        self.close()
        self.spilled_rows = self.partitions_made = self.sort_fallbacks = 0
        if self.by_sort:
            self.sorter.open()
            self.out = self._adjacent(self.sorter._next)
        elif not self.grouped:
            self.source.open()
            self.out = self._one_group()
        else:
            self.source.open()
            self._absorb(self.source.next, self.first_table, 0)
            shut(self.source)

    def _next(self):
        while True:
            row = next(self.out, None)
            if row is not None:
                return row
            self._finish_batch()
            if not self.pending:
                return None
            self._advance()

    def _finish_batch(self):
        """The table (or the fallback sort) that was being output is finished: free its frames."""
        if self.table_ws is not None:
            self.table_ws.close()
            self.table_ws = None
        if self.fallback is not None:
            sorter, file = self.fallback
            sorter.close()
            file.delete()
            self.fallback = None

    def close(self):
        if self.out is not None:
            if hasattr(self.out, 'close'):
                self.out.close()
            self.out = None
        self._finish_batch()
        for _, file, _ in self.pending:
            file.delete()
        self.pending = []
        if self.sorter is not None:
            self.sorter.close()
        shut(self.source)

    # ---------------------------------------------------------------- one group, sorted groups

    def _one_group(self):
        state = self.aggs.start()
        while True:
            row = self.source.next()
            if row is None:
                break
            self.aggs.update(state, row)
        yield self.aggs.finish(state)

    def _adjacent(self, pull):
        """Groups of a stream that is sorted on the grouping columns."""
        key_of, aggs, c = self.key_of, self.aggs, self.c
        row = pull()
        while row is not None:
            key = key_of(row)
            state = aggs.start()
            aggs.update(state, row)
            row = pull()
            while row is not None:
                c.comparisons += 1                  # does this row still belong to the group?
                if key_of(row) != key:
                    break
                aggs.update(state, row)
                row = pull()
            yield key + aggs.finish(state)

    # ---------------------------------------------------------------- hash mode

    def _absorb(self, pull, table_frames, level):
        """Group the rows `pull` returns in a table of `table_frames` frames. Rows whose group
        does not fit go to a spill file, which is queued for another round."""
        ws = Workspace(self.ctx, table_frames)
        spill = None
        try:
            spill = RunWriter(self.ctx, self.in_codec, self.c)
            table = {}
            key_of, aggs, c = self.key_of, self.aggs, self.c
            entry_extra = aggs.state_size
            key_size = self.key_codec.size
            while True:
                row = pull()
                if row is None:
                    break
                key = key_of(row)
                c.comparisons += 1                  # one look-up of the key in the table
                state = table.get(key)
                if state is None:
                    if not ws.add(key_size(key) + entry_extra):
                        spill.add(row)              # the table is full and this group is not in it
                        self.spilled_rows += 1
                        continue
                    state = table[key] = aggs.start()
                aggs.update(state, row)
        except BaseException:
            ws.close()
            if spill is not None:
                spill.abandon()
            raise
        file = spill.close()
        if file is not None:
            self.pending.append(('spill', file, level))
        self.table_ws = ws
        self.out = (key + aggs.finish(state) for key, state in table.items())

    def _advance(self):
        """Take the next file that still holds ungrouped rows and deal with it."""
        kind, file, level = self.pending.pop(0)
        if kind == 'part':                          # a partition: group it with a fresh table
            reader = RunReader(self.ctx, file, self.in_codec, self.c)
            try:
                self._absorb(reader.next, self.hold - 2, level)
            finally:
                reader.close()
                file.delete()
        elif level >= MAX_LEVEL:                    # split often enough: sort it instead
            self.sort_fallbacks += 1
            sorter = Sort(self.ctx, FileScan(self.ctx, file, self.child.columns, self.c),
                          [(i, False) for i in self.key_idx], self.where, counters=self.c)
            self.fallback = (sorter, file)
            sorter.assign(self.hold, self.hold)
            sorter.open()
            self.out = self._adjacent(sorter._next)
        else:                                       # a spill file: split it by hash of the key
            reader = RunReader(self.ctx, file, self.in_codec, self.c)
            try:
                parts = partition(self.ctx, reader.next, self.in_codec, self.key_of,
                                  self.hold - 1, level, self.c)
            finally:
                reader.close()
                file.delete()
            parts = [p for p in parts if p is not None]
            self.partitions_made += len(parts)
            self.pending[0:0] = [('part', p, level + 1) for p in parts]
            self.out = iter(())

    def details(self):
        if self.by_sort:
            return dict(self.sorter.details(), mode='sort')
        if not self.grouped:
            return {'mode': 'one group'}
        return {'mode': 'hash', 'table_frames': self.first_table, 'spilled_rows': self.spilled_rows,
                'partitions': self.partitions_made, 'sort_fallbacks': self.sort_fallbacks}


class Distinct(GroupBy):
    """Removes duplicate rows: group_by on every column, with no aggregates."""
    name = 'distinct'

    def __init__(self, ctx, child, mode, where, counters=None):
        super().__init__(ctx, child, mode, list(range(len(child.columns))), [], where, counters)
