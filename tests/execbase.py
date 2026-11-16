"""Base class for the execution engine tests.

setUpClass builds one small database on disk. run() executes a physical plan against it with a
given number of frames and returns the rows and the Execution (for counters and details).
reference() runs the same plan on the Bonus Project 1 engine, in memory and with no budget, so
a result can be checked against an independent implementation.
"""
import shutil
import tempfile
import unittest
from collections import Counter
from executor.context import ExecError
from executor.run import Execution
from source.engine import evaluate, MemorySource
from source.operators import Relation, SchemaAttr, NUMBER, STRING
from source.plan_json import from_json
from store.db import Database

INTS = [('a', 'INT', None), ('b', 'INT', None), ('c', 'INT', None)]

# name -> (columns, rows)
TABLES = {
    # 8 full pages of 255 rows. a is a permutation of 0..2039, b has 50 values, c is the row number.
    'R': (INTS, [((i * 7919) % 2040, i % 50, i) for i in range(2040)]),
    # 300 rows on one page; b has 60 values, so R.b = S.b matches 5 rows of S for 50 of them.
    'S': ([('b', 'INT', None), ('d', 'VARCHAR', 10)], [(i % 60, f'v{i}') for i in range(300)]),
    # Wide rows, 19 to a page: 30 pages. k has 95 values.
    'W': ([('k', 'INT', None), ('pad', 'VARCHAR', 200)], [(i % 95, f'{i:05d}' + 'x' * 195) for i in range(570)]),
    'V': ([('k', 'INT', None), ('pad', 'VARCHAR', 200)], [(i % 40, f'{i:05d}' + 'y' * 195) for i in range(190)]),
    # Heavy duplicates: 12 distinct rows among 600.
    'T': ([('x', 'INT', None), ('s', 'VARCHAR', 8)], [(i % 4, 'abc'[i % 3]) for i in range(600)]),
    # One key value in 60 of 100 wide rows: more than three pages of rows that all hash alike.
    'H': ([('k', 'INT', None), ('pad', 'VARCHAR', 200)],
          [(0 if i % 5 < 3 else i, f'{i:05d}' + 'h' * 195) for i in range(100)]),
    'E': ([('a', 'INT', None), ('b', 'INT', None)], []),
    # The duplicate-key example of the specification.
    'L4': ([('b', 'INT', None)], [(1,), (2,), (2,), (3,)]),
    'R3': ([('b', 'INT', None)], [(2,), (2,), (4,)]),
    'Emp': ([('eid', 'INT', None), ('name', 'VARCHAR', 10), ('mgr', 'INT', None), ('salary', 'INT', None)],
            [(1, 'Ann', 1, 500), (2, 'Bob', 1, 300), (3, 'Cy', 2, 700), (4, 'Di', 2, 200)]),
}


def scan(table):
    return {'op': 'seqscan', 'table': table}


def join(kind, condition, left, right):
    return {'op': kind + '_join', 'condition': condition,
            'left': scan(left) if isinstance(left, str) else left,
            'right': scan(right) if isinstance(right, str) else right}


def bag(rows):
    return Counter(rows)


def to_logical(plan):
    """The logical plan that means the same as a physical plan."""
    op = plan['op']
    if op == 'seqscan':
        return {'op': 'scan', 'table': plan['table']}
    inputs = {k: to_logical(plan[k]) for k in ('child', 'left', 'right') if k in plan}
    if op == 'filter':
        return {'op': 'select', 'condition': plan['condition'], **inputs}
    if op == 'distinct':
        return {'op': 'distinct', **inputs}
    if op == 'group_by':
        aggregates = [[a[0], a[1], a[2] if len(a) == 3 else f'agg{i}'] for i, a in enumerate(plan['aggregates'])]
        return {'op': 'group_by', 'keys': plan['keys'], 'aggregates': aggregates, **inputs}
    if op.endswith('_join'):
        return {'op': 'join', 'condition': plan['condition'], **inputs}
    return {**{k: v for k, v in plan.items() if k not in inputs}, **inputs}


class ExecTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        db = Database(cls.dir, frames=16)
        for name, (columns, rows) in TABLES.items():
            heap = db.create_table(name, columns)
            for row in rows:
                heap.insert(row)
        cls.pages = {name: db.catalog.get_table(name).num_pages for name in TABLES}
        db.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_plan(self, plan, frames=4, policy='lru'):
        """-> (rows, execution). Checks the budget and the clean-up on the way."""
        db = Database(self.dir, frames, policy)
        try:
            execution = Execution(db, plan)
            rows = list(execution.rows())
            self.assertLessEqual(db.pool.high_water, frames)
            self.assertEqual((db.pool.pinned, len(db.pool.reserved), db.disk.live_temps), (0, 0, 0))
            return rows, execution
        finally:
            db.disk.close()

    def rows(self, plan, frames=4):
        return self.run_plan(plan, frames)[0]

    def fails(self, plan, frames=4, error=ExecError):
        db = Database(self.dir, frames)
        try:
            with self.assertRaises(error) as cm:
                list(Execution(db, plan).rows())
            self.assertEqual((db.pool.pinned, len(db.pool.reserved), db.disk.live_temps), (0, 0, 0))
            return str(cm.exception)
        finally:
            db.disk.close()

    def reference(self, plan):
        relations = {}
        for name, (columns, rows) in TABLES.items():
            schema = [SchemaAttr(name, c, NUMBER if kind == 'INT' else STRING) for c, kind, _ in columns]
            relations[name] = Relation(name, schema, rows)
        return list(evaluate(from_json(to_logical(plan)), MemorySource(relations)).rows)

    def assertSameBag(self, plan, frames=(4,)):
        """The plan returns what the reference engine returns, at every given budget."""
        expected = bag(self.reference(plan))
        for b in frames:
            with self.subTest(frames=b):
                self.assertEqual(bag(self.rows(plan, b)), expected)
        return expected
