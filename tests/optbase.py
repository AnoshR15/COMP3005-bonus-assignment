"""Base class for the optimizer tests: a small analyzed database, and helpers that take a query
from SQL to a logical plan, to a physical plan, to rows, and to the rows the Bonus Project 1
engine gets from the logical plan directly."""
import random
import shutil
import tempfile
import unittest
from collections import Counter
from executor.run import Execution
from optimizer.planner import Planner
from optimizer.stats import analyze
from source.engine import evaluate, MemorySource
from source.operators import Relation, SchemaAttr, NUMBER, STRING
from source.plan_json import from_json
from sqlfront.catalog import load_catalog
from sqlfront.translate import compile_sql
from store.db import Database

rng = random.Random(7)
CITIES = [f'city{i}' for i in range(12)]
TABLES = {
    'Emp': ([('eid', 'INT', None), ('name', 'VARCHAR', 20), ('age', 'INT', None), ('salary', 'INT', None),
             ('did', 'INT', None), ('mgr', 'INT', None), ('city', 'VARCHAR', 20)],
            [(i, f'emp{i}', 20 + i % 51, rng.randrange(100, 5000), i % 20, rng.randrange(600), CITIES[i % 12])
             for i in range(600)]),
    'Dept': ([('did', 'INT', None), ('title', 'VARCHAR', 20), ('budget', 'INT', None), ('city', 'VARCHAR', 20)],
             [(i, f'dept{i}', 100 * i, CITIES[i % 12]) for i in range(20)]),
    'Proj': ([('pid', 'INT', None), ('did', 'INT', None), ('pname', 'VARCHAR', 30)],
             [(i, i % 20, f'project{i}') for i in range(40)]),
    'Works': ([('eid', 'INT', None), ('pid', 'INT', None), ('hours', 'INT', None)],
              [(rng.randrange(600), rng.randrange(40), rng.randrange(1, 40)) for _ in range(900)]),
    # most rows have status 0: a value common enough to get a histogram bucket to itself
    'Skew': ([('k', 'INT', None), ('status', 'INT', None)],
             [(i, 0 if i % 10 < 7 else i % 10) for i in range(1000)]),
    'Empty': ([('a', 'INT', None), ('b', 'VARCHAR', 5)], []),
    # wide rows, 19 to a page: 30 pages and 20 pages, enough for the choice of join to depend on B
    'W1': ([('k', 'INT', None), ('pad', 'VARCHAR', 200)], [(i, f'{i:04d}' + 'a' * 196) for i in range(570)]),
    'W2': ([('k', 'INT', None), ('pad', 'VARCHAR', 200)], [(i, f'{i:04d}' + 'b' * 196) for i in range(380)]),
    'A': ([('x', 'INT', None), ('y', 'INT', None)], [(i, i) for i in range(5)]),
    'Bp': ([('x', 'INT', None), ('y', 'INT', None)], [(i, i) for i in range(5)]),
    'C': ([('x', 'INT', None), ('y', 'INT', None)], [(i, i) for i in range(5)]),
}
ANALYZED = ('Emp', 'Dept', 'Proj', 'Works', 'Skew', 'Empty', 'W1', 'W2')      # A, Bp and C never are


def bag(rows):
    return Counter(rows)


class OptTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        db = Database(cls.dir, frames=16)
        for name, (columns, rows) in TABLES.items():
            heap = db.create_table(name, columns)
            for row in rows:
                heap.insert(row)
        db.flush()
        for name in ANALYZED:
            analyze(db, name)
        db.close()
        cls.sql_catalog = load_catalog(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def logical(self, sql):
        plans = compile_sql(sql, self.sql_catalog)
        self.assertEqual(len(plans), 1)
        return plans[0]

    def optimize(self, logical, frames=8, disabled=(), assume=None):
        if isinstance(logical, str):
            logical = self.logical(logical)
        db = Database(self.dir, 8)
        try:
            return Planner(db.catalog, frames, disabled, assume).optimize(logical)
        finally:
            db.disk.close()

    def execute(self, physical, frames=8):
        """-> (rows, execution); the plan goes in exactly as the optimizer printed it."""
        db = Database(self.dir, frames)
        try:
            execution = Execution(db, physical)
            rows = list(execution.rows())
            self.assertLessEqual(db.pool.high_water, frames)
            return rows, execution
        finally:
            db.disk.close()

    def reference(self, logical):
        """The rows the logical plan means, computed in memory with no optimizer and no budget."""
        relations = {}
        for name, (columns, rows) in TABLES.items():
            schema = [SchemaAttr(name, c, NUMBER if kind == 'INT' else STRING) for c, kind, _ in columns]
            relations[name] = Relation(name, schema, rows)
        return list(evaluate(from_json(logical), MemorySource(relations)).rows)
