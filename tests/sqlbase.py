"""Base class for the SQL front end tests: a catalog, a little data, and helpers that compile a
query, catch its error, or run its plan on the data."""
import os
import unittest
from tests.base import DATA
from source.engine import evaluate, MemorySource
from source.operators import Relation, SchemaAttr, NUMBER, STRING
from source.plan_json import from_json
from sqlfront.catalog import load_catalog
from sqlfront.errors import SqlError
from sqlfront.translate import compile_sql

DB = os.path.join(DATA, 'sqldb')

# Rows for the tables of tests/data/sqldb/catalog.json. Emp holds one exact duplicate pair in
# (name, age): two Bobs aged 41.
ROWS = {
    #        eid name    age salary did mgr city
    'Emp': [(1, 'Ann', 31, 500, 10, 1, 'Ottawa'),
            (2, 'Bob', 41, 300, 10, 1, 'Ottawa'),
            (3, 'Bob', 41, 700, 20, 2, 'Toronto'),
            (4, 'Cy', 22, 200, 20, 2, 'Toronto'),
            (5, 'Di', 55, 900, 30, 1, 'Ottawa')],
    #         did title   budget city
    'Dept': [(10, 'Eng', 5000, 'Ottawa'),
             (20, 'Ops', 800, 'Toronto'),
             (30, 'Law', 2000, 'Hull')],
    #         pid did pname
    'Proj': [(100, 10, 'Compiler'), (200, 20, 'Backup')],
    #          eid pid hours
    'Works': [(1, 100, 5), (2, 100, 7), (3, 200, 9)],
}


def walk(plan):
    """Every node of a plan, parents first."""
    yield plan
    for field in ('child', 'left', 'right'):
        if field in plan:
            yield from walk(plan[field])


def ops(plan):
    """The operator names of a plan, top to bottom, left before right."""
    return [node['op'] for node in walk(plan)]


class SqlTestCase(unittest.TestCase):

    def catalog(self):
        return load_catalog(DB)

    def compile(self, sql):
        """The plan of a single statement."""
        plans = compile_sql(sql, self.catalog())
        self.assertEqual(len(plans), 1)
        return plans[0]

    def error(self, sql, category):
        """Compile sql, expect an error of this category, and return it."""
        with self.assertRaises(SqlError) as cm:
            compile_sql(sql, self.catalog())
        self.assertEqual(cm.exception.category, category, str(cm.exception))
        return cm.exception

    def source(self, rows=None):
        rows = rows or ROWS
        relations = {}
        catalog = self.catalog()
        for name in catalog.list_tables():
            schema = [SchemaAttr(name, c.name, NUMBER if c.kind == 'INT' else STRING)
                      for c in catalog.get_table(name)]
            relations[name] = Relation(name, schema, rows.get(name, []))
        return MemorySource(relations)

    def run_sql(self, sql, rows=None):
        """Compile, load the plan through the JSON reader, and run it. Rows in output order."""
        result = evaluate(from_json(self.compile(sql)), self.source(rows))
        return list(result.rows)
