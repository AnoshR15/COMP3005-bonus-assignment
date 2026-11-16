"""The integration workload: the order database loaded through the prompt, and its 25 queries.

No dataset and no queries were published for the integration checkpoint, so workload/ holds my
own. Here every query is run through the whole system and its answer compared with the Bonus
Project 1 engine evaluating the same logical plan in memory: an implementation that shares no
code with the storage engine, the executor or the optimizer.
"""
import contextlib
import csv
import os
import shutil
import tempfile
import unittest
from bench.m4_measure import order_tables
from minidb import Session, split_statements, _Capture
from source.engine import evaluate, MemorySource
from source.operators import Relation, SchemaAttr, NUMBER, STRING
from source.plan_json import from_json
from sqlfront.catalog import load_catalog
from sqlfront.translate import compile_sql
from tests.base import ROOT
from workload.answers import run, normal, same_answer, compare
from workload.build import queries

WORKLOAD = os.path.join(ROOT, 'workload')


class WorkloadTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """The database is built the way the report says: by the statements of load.sql, in the
        prompt. Only the path of the CSV files is made absolute."""
        cls.dir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.dir, 'wdb')
        with open(os.path.join(WORKLOAD, 'load.sql'), encoding='utf8') as f:
            script = f.read().replace("'workload/data/", "'" + os.path.join(WORKLOAD, 'data', ''))
        session = Session(cls.db, 32, out=_Capture())
        cls.load_ok = all([session.run(statement) for statement in split_statements(script)])
        cls.load_output = session.out.text

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_the_data_files_are_the_generator_s_rows(self):
        for name, _, rows in order_tables():
            with open(os.path.join(WORKLOAD, 'data', name + '.csv'), newline='', encoding='utf8') as f:
                self.assertEqual(list(csv.reader(f)), [[str(v) for v in row] for row in rows], name)

    def test_load_sql_builds_the_database(self):
        self.assertTrue(self.load_ok, self.load_output)
        for line in ('loaded 300 rows into Customer: 300 rows on 7 pages',
                     'loaded 2000 rows into Orders: 2000 rows on 40 pages',
                     'loaded 3000 rows into Item: 3000 rows on 42 pages',
                     'analyzed Supply: 500 rows, 2 pages, 3 columns'):
            self.assertIn(line, self.load_output)

    def test_there_are_twenty_five_queries_and_the_postgres_file_has_the_same_ones(self):
        all_queries = queries()
        self.assertEqual(len(all_queries), 25)
        with open(os.path.join(WORKLOAD, 'postgres_queries.sql'), encoding='utf8') as f:
            copies = [line for line in f.read().splitlines() if line.startswith('\\copy ')]
        self.assertEqual(copies, [f"\\copy ({sql}) TO 'workload/postgres/w{n:02d}.csv' WITH (FORMAT csv)"
                                  for n, sql in enumerate(all_queries, start=1)])

    def test_every_answer_is_the_reference_engine_s_answer_at_every_budget(self):
        relations = {}
        for name, schema, rows in order_tables():
            attrs = [SchemaAttr(name, c, NUMBER if kind == 'INT' else STRING) for c, kind, _ in schema]
            relations[name] = Relation(name, attrs, rows)
        catalog = load_catalog(self.db)
        expected = []
        for sql in queries():
            logical = compile_sql(sql, catalog)[0]
            rows = evaluate(from_json(logical), MemorySource(relations)).rows
            expected.append([[normal(v) for v in row] for row in rows])
        self.assertEqual([len(rows) for rows in expected],
                         [39, 46, 72, 30, 5, 7, 1, 1, 5, 11, 6, 3, 6, 29, 52, 2, 1, 52, 2, 6, 14, 330, 6, 8, 24])
        self.assertEqual(expected[6], [['0']])                          # W07: COUNT(*) over no rows
        for frames in (4, 8, 32):
            for number, ((sql, rows), want) in enumerate(zip(run(self.db, frames), expected), start=1):
                with self.subTest(frames=frames, query=f'W{number:02d}'):
                    self.assertTrue(same_answer(sql, rows, want), (rows[:3], want[:3]))

    def test_comparing_two_sets_of_answers(self):
        """An average with four decimals here and sixteen there is the same answer; a missing row
        is not; and an ORDER BY query with its rows in another order is not."""
        self.assertEqual(normal('2482.7846153846153'), normal(2482.7846153846153))      # sixteen decimals, or four
        self.assertEqual(normal('2482.7846153846153'), '2482.7846')
        self.assertEqual((normal('007'), normal(-5), normal('cust007'), normal('1.5.2')), ('7', '-5', 'cust007', '1.5.2'))
        bag_query, ordered_query = 'SELECT a FROM T', 'SELECT a FROM T ORDER BY a'
        rows = [['1', 'x'], ['2', 'y'], ['2', 'y']]
        backwards = list(reversed(rows))
        self.assertTrue(same_answer(bag_query, rows, backwards))                # a bag has no order
        self.assertFalse(same_answer(ordered_query, rows, backwards))           # an ORDER BY answer has
        self.assertFalse(same_answer(bag_query, rows, rows[:2]))                # one copy of a duplicate missing
        self.assertEqual([n for n, sql in enumerate(queries(), start=1) if ' ORDER BY ' in sql.upper()], [4, 5, 6, 11])

        ours, theirs = os.path.join(self.dir, 'ours'), os.path.join(self.dir, 'theirs')
        for directory in (ours, theirs):
            os.makedirs(directory)
            for number, (_, answer) in enumerate(run(self.db, 32), start=1):
                with open(os.path.join(directory, f'w{number:02d}.csv'), 'w', newline='') as f:
                    csv.writer(f, lineterminator='\n').writerows(answer)
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet):
            self.assertEqual(compare(ours, theirs), 0)
            os.remove(os.path.join(theirs, 'w03.csv'))
            self.assertEqual(compare(ours, theirs), 1)                          # a missing answer is a difference
