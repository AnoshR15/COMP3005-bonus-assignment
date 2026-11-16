"""union, intersect and except. All three have set semantics: no duplicates in the result.

Both inputs must have the same number of columns with the same types position by position;
the names may differ and the result takes the left names.

    union       all rows of both inputs, then duplicate removal by sorting: the same code as
                distinct in sort mode, over the same code as union_all
    intersect   a row appears once if it occurs at least once on each side
    except      a row appears once if it occurs on the left and not on the right

intersect and except sort both inputs on all columns with the external sort and walk the two
sorted streams in step, so neither input is ever held in memory.
"""
from .groupby import Distinct
from .operators import Operator, UnionAll, check_same_types, merged_columns
from .sort import Sort


class Union(Operator):
    name = 'union'

    def __init__(self, ctx, left, right, where):
        both = UnionAll(ctx, left, right, where)
        super().__init__(ctx, both.columns, [left, right], where)
        self.inner = Distinct(ctx, both, 'sort', where, counters=self.c)
        self.min_hold = self.inner.min_hold

    def assign(self, frames, limit):
        self.inner.assign(frames, limit)
        self.frames, self.hold = frames, self.inner.hold

    def open(self):
        self.inner.open()

    def next(self):
        return self.inner.next()            # counted there, on the counters the two share

    def close(self):
        self.inner.close()

    def details(self):
        return self.inner.details()


class SortedSetOp(Operator):
    """intersect and except."""
    min_hold = 2

    def __init__(self, ctx, left, right, where):
        check_same_types(left.columns, right.columns, where)
        super().__init__(ctx, merged_columns(left.columns, right.columns), [left, right], where)
        keys = [(i, False) for i in range(len(self.columns))]
        self.left_sort = Sort(ctx, left, keys, where, self.c, on_disk=True)
        self.right_sort = Sort(ctx, right, keys, where, self.c, on_disk=True)
        self.out = None

    def assign(self, frames, limit):
        self.need(frames, limit, 3, 2)
        each = min(frames, limit) // 2          # the two last merges run side by side
        self.left_sort.assign(frames, each)
        self.right_sort.assign(frames, each)
        self.frames, self.hold = frames, self.left_sort.hold + self.right_sort.hold

    def open(self):
        self.close()
        self.left_sort.open()                   # holds nothing afterwards: its runs are on disk
        self.right_sort.open()
        self.out = self._walk()

    def _walk(self):
        left, right, c = self.left_sort, self.right_sort, self.c
        keep_if_found = self.name == 'intersect'
        row, other = left._next(), right._next()
        while row is not None:
            while other is not None:            # move the right side up to this row
                c.comparisons += 1
                if other >= row:
                    break
                other = right._next()
            if (other == row) == keep_if_found:
                yield row
            previous = row
            row = left._next()
            while row is not None:              # skip the copies of the row just handled
                c.comparisons += 1
                if row != previous:
                    break
                row = left._next()

    def _next(self):
        return next(self.out, None)

    def close(self):
        if self.out is not None:
            self.out.close()
            self.out = None
        self.left_sort.close()
        self.right_sort.close()

    def details(self):
        return {'left_sort': self.left_sort.details(), 'right_sort': self.right_sort.details()}


class Intersect(SortedSetOp):
    name = 'intersect'


class Except(SortedSetOp):
    name = 'except'
