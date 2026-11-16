"""The sample database, the sample plans and the sample queries: everything in samples/ works.

No dataset, plan file or SQL file was published with the specifications, so samples/ holds my
own. This test builds the database the way `python -m samples.build` does and runs every file.
"""
import glob
import json
import os
import shutil
import tempfile
import unittest
from collections import Counter
from executor.context import ExecError
from executor.run import Execution, load_plan
from optimizer.planner import Planner
from samples.build import build, tables
from sqlfront.catalog import load_catalog
from sqlfront.translate import compile_sql
from store.db import Database
from tests.base import ROOT

PLANS = os.path.join(ROOT, 'samples', 'plans')


class SampleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.dir, 'db')
        build(cls.db, os.path.join(cls.dir, 'data'), say=lambda line: None)
        cls.rows = {name: rows for name, _, rows in tables()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def plan(self, name):
        with open(os.path.join(PLANS, name + '.json'), encoding='utf8') as f:
            return load_plan(f.read())

    def run_plan(self, plan, frames):
        db = Database(self.db, frames)
        try:
            execution = Execution(db, plan)
            rows = list(execution.rows())
            self.assertLessEqual(db.pool.high_water, frames)
            self.assertEqual((db.pool.pinned, len(db.pool.reserved), db.disk.live_temps), (0, 0, 0))
            return rows, execution
        finally:
            db.disk.close()

    def test_the_tables_are_the_sizes_the_documentation_says(self):
        db = Database(self.db, 8)
        sizes = {name: (db.catalog.get_table(name).num_tuples, db.catalog.get_table(name).num_pages)
                 for name in db.catalog.list_tables()}
        analyzed = all(db.catalog.get_table(name).stats is not None for name in sizes)
        db.disk.close()
        self.assertEqual(sizes, {'R': (3000, 13), 'S': (400, 2), 'Emp': (600, 6), 'Dept': (20, 1), 'DupL': (4, 1),
                                 'DupR': (3, 1), 'Empty': (0, 0), 'Wide': (1520, 80), 'Wide2': (380, 20)})
        self.assertTrue(analyzed)

    def test_every_sample_plan_runs_in_4_frames_and_in_32(self):
        files = sorted(glob.glob(os.path.join(PLANS, '*.json')))
        self.assertEqual(len(files), 21)
        for path in files:
            name = os.path.basename(path)[:-5]
            with self.subTest(plan=name):
                small, _ = self.run_plan(self.plan(name), 4)
                large, _ = self.run_plan(self.plan(name), 32)
                self.assertEqual(Counter(small), Counter(large))        # B changes the cost, not the answer

    def test_what_some_of_them_return(self):
        R, S = self.rows['R'], self.rows['S']
        rows, _ = self.run_plan(self.plan('hash_join'), 32)             # the plan of the handout
        self.assertEqual(Counter(rows), Counter(r + s for r in R if r[0] > 500 for s in S if r[1] == s[0]))
        self.assertEqual(sorted(self.run_plan(self.plan('duplicate_keys'), 4)[0]), [(2, 2)] * 4)
        self.assertEqual(self.run_plan(self.plan('count_of_empty'), 4)[0], [(0,)])
        self.assertEqual(len(self.run_plan(self.plan('limit'), 4)[0]), 5)
        rows, execution = self.run_plan(self.plan('sort_20xB'), 4)      # 80 pages in 4 frames
        self.assertEqual([r[0] for r in rows], list(range(1520)))
        self.assertEqual(execution.totals()['page_reads'], 312)         # as in REPORT_M2.md, section 2
        rows, _ = self.run_plan(self.plan('nested_loop_join'), 4)
        self.assertEqual(Counter(rows), Counter(self.run_plan(self.plan('sort_merge_join'), 4)[0]))
        cities = {r[6] for r in self.rows['Emp']}, {r[3] for r in self.rows['Dept']}
        self.assertEqual(len(self.run_plan(self.plan('union_all'), 4)[0]), 620)
        self.assertEqual(set(self.run_plan(self.plan('union'), 4)[0]), {(c,) for c in cities[0] | cities[1]})
        self.assertEqual(set(self.run_plan(self.plan('intersect'), 4)[0]), {(c,) for c in cities[0] & cities[1]})
        self.assertEqual(set(self.run_plan(self.plan('except'), 4)[0]), {(c,) for c in cities[1] - cities[0]})

    def test_every_bad_plan_is_rejected_with_one_line(self):
        files = sorted(glob.glob(os.path.join(PLANS, 'bad', '*.json')))
        self.assertEqual(len(files), 9)
        expected = {'missing_field': "Node 'sort' at $: missing required field 'keys'",
                    'wrong_type': "Node 'filter' at $: field 'condition' must be a string",
                    'unknown_operator': "unknown operator 'merge_join'",
                    'unknown_table': "unknown table 'T'",
                    'unknown_column': 'R.zzz',
                    'type_mismatch': 'cannot compare number to string',
                    'inequality_in_hash_join': 'hash_join needs equalities',
                    'sum_of_empty': 'sum(Empty.a) of an empty input is undefined',
                    'not_json': 'the plan is not valid JSON'}
        for path in files:
            name = os.path.basename(path)[:-5]
            with self.subTest(plan=name):
                db = Database(self.db, 8)
                try:
                    with self.assertRaises(ExecError) as cm:
                        with open(path, encoding='utf8') as f:
                            list(Execution(db, load_plan(f.read())).rows())
                finally:
                    db.disk.close()
                self.assertIn(expected[name], str(cm.exception))
                self.assertEqual(len(str(cm.exception).splitlines()), 1)

    def test_the_section_5_queries_compile_plan_and_run(self):
        with open(os.path.join(ROOT, 'samples', 'section5.sql'), encoding='utf8') as f:
            logical = compile_sql(f.read(), load_catalog(self.db))
        self.assertEqual([p['op'] for p in logical],
                         ['project', 'project', 'project', 'limit', 'project', 'project', 'union_all', 'union'])
        counts = []
        for plan in logical:
            db = Database(self.db, 8)
            physical = Planner(db.catalog, 8).optimize(plan).plan.to_json()
            db.disk.close()
            counts.append(len(self.run_plan(json.loads(json.dumps(physical)), 8)[0]))
        E, D = self.rows['Emp'], self.rows['Dept']
        self.assertEqual(counts[0], sum(1 for e in E if e[2] > 30))
        self.assertEqual(counts[1], sum(1 for e in E for d in D if e[4] == d[0] and d[2] > 1000))
        self.assertEqual(counts[2], 600)                                # every employee has a department
        self.assertEqual(counts[4], 600)                                # and a manager
        self.assertEqual(counts[6], 620)
        self.assertEqual(counts[7], len({e[6] for e in E} | {d[3] for d in D}))
