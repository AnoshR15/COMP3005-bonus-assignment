"""Running a physical plan.

    execution = Execution(db, plan)       validate, build, divide the frames; nothing is read yet
    for row in execution.rows(): ...      open, pull rows from the root, close
    execution.totals()                    the four counters summed over all operators
    execution.trace()                     one line per operator, in plan order; page counts of a
                                          line include everything below it (own=True: its own)

The frame budget B is the size of the database's buffer pool. After the last row, rows() checks
that no page is still pinned, no frame is still reserved and no temporary file is left, and
raises if any is: a plan that leaks is wrong even when its rows were right.
"""
import json
from .build import validate, build
from .context import ExecContext, ExecError, BudgetError, PlanError
from .operators import type_text
from store.record import VARCHAR


def load_plan(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError(f'the plan is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}') from None


class Execution:

    def __init__(self, db, plan):
        self.db = db
        self.frames = db.pool.size
        validate(plan)
        self.root = build(plan, ExecContext(db))
        try:
            self.root.assign(self.frames, self.frames)
        except BudgetError as e:
            raise BudgetError(f'this plan cannot run in {self.frames} frames'
                              f'{self._enough()} ({e})') from None

    def _enough(self):
        for frames in range(self.frames + 1, self.frames + 33):
            try:
                self.root.assign(frames, frames)
                return f'; it needs at least {frames}'
            except BudgetError:
                pass
        return ''

    def schema(self):
        """[["R.a", "INT"], ["S.c", "VARCHAR", 20]]"""
        return [[c.name, c.kind, c.size] if c.kind == VARCHAR else [c.name, c.kind] for c in self.root.columns]

    def rows(self):
        pool, disk = self.db.pool, self.db.disk
        try:
            self.root.open()
            while True:
                row = self.root.next()
                if row is None:
                    break
                yield row
        finally:
            self.root.close()
        if pool.pinned or pool.reserved or disk.live_temps:
            raise ExecError(f'internal error: the plan finished with {pool.pinned} pages pinned, '
                            f'{len(pool.reserved)} frames reserved and {disk.live_temps} temporary files')

    def started(self):
        """The same rows as rows(), but the plan is opened and its first row fetched before this
        returns. Whatever a plan raises while it runs, it raises in open() or on its first row
        (an aggregate of an empty input, a row too wide for a page of a temporary file), so the
        error arrives here, before the caller has printed a header it cannot take back."""
        rows = self.rows()
        nothing = object()
        first = next(rows, nothing)

        def from_the_first():
            if first is not nothing:
                yield first
                yield from rows
        return from_the_first()

    def operators(self):
        """(depth, operator) in plan order: a node, then its inputs left to right."""
        def walk(op, depth):
            yield depth, op
            for child in op.children:
                yield from walk(child, depth + 1)
        return list(walk(self.root, 0))

    def totals(self):
        total = {'tuples_out': self.root.c.tuples_out, 'comparisons': 0, 'page_reads': 0, 'page_writes': 0}
        for _, op in self.operators():
            for name in ('comparisons', 'page_reads', 'page_writes'):
                total[name] += getattr(op.c, name)
        return total

    def peak_frames(self):
        return self.db.pool.high_water

    def trace(self, own=False):
        """One line per operator, in plan order, indented by depth.

        tuples_out and comparisons are the operator's own. reads and writes are those of the
        operator and of everything below it, as in the example of the specification: a hash join
        over two 24-page scans shows 96 reads, the scans' 48 and the 48 it read back from its
        partitions. The top line is therefore the whole plan. With own=True reads and writes
        are the operator's alone, which is what counters() returns."""
        ops = self.operators()
        labels = ['  ' * depth + op.label for depth, op in ops]
        width = max(len(label) for label in labels) + 2
        lines = []
        for label, (_, op) in zip(labels, ops):
            c = op.counters()
            reads, writes = c['page_reads'], c['page_writes']
            if not own:
                reads, writes = self.below(op)
            lines.append(f"{label:<{width}} tuples_out={c['tuples_out']:<8} comparisons={c['comparisons']:<10} "
                         f"reads={reads:<7} writes={writes}")
        return lines

    def below(self, op):
        reads, writes = op.c.page_reads, op.c.page_writes
        for child in op.children:
            r, w = self.below(child)
            reads, writes = reads + r, writes + w
        return reads, writes

    def report(self):
        """Everything measured, as one JSON-ready object: used by the reports."""
        def node(op):
            reads, writes = self.below(op)
            return {'op': op.label, 'schema': [f'{c.name}:{type_text(c)}' for c in op.columns],
                    'frames_while_streaming': op.hold, **op.counters(),
                    'subtree_page_reads': reads, 'subtree_page_writes': writes, 'details': op.details(),
                    'inputs': [node(child) for child in op.children]}
        return {'frames': self.frames, 'peak_frames': self.peak_frames(), 'totals': self.totals(),
                'plan': node(self.root)}


def format_value(value):
    """A value as JSON. A real number (only AVG produces one) is written to four decimal places."""
    if isinstance(value, float):
        return f'{value:.4f}'
    return json.dumps(value, ensure_ascii=False)


def format_row(row):
    return '[' + ', '.join(format_value(v) for v in row) + ']'
