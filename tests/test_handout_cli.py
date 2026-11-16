"""The command lines of the Project 2 handout, typed the way the handout prints them.

Every tool runs from a working directory that holds the database in ./db, with the handout's own
options and no others: no --db for storage and exec, --catalog db/ for sql and opt, and no
--frames for opt dp-table. A change that turns any of these into a usage error fails here.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from tests.base import ROOT

# Section 3.2 of the handout, character for character.
PLAN = """{
  "op": "hash_join",
  "condition": "R.b = S.b",
  "left":  { "op": "filter",
             "condition": "R.a > 500",
             "child": { "op": "seqscan", "table": "R" } },
  "right": { "op": "seqscan", "table": "S" }
}"""

QUERY = "SELECT r.a, s.d FROM R r JOIN S s ON r.b = s.b WHERE r.a > 500;"

R_ROWS = [(i, f'k{i % 50}', i % 7) for i in range(1500)]
S_ROWS = [(f'k{i % 25}', i) for i in range(100)]
JOINED = sum(1 for a, b, _ in R_ROWS if a > 500 for sb, _ in S_ROWS if sb == b)


class HandoutCommandLines(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = cls._tmp.name
        cls.write('r.csv', ''.join(f'{a},{b},{c}\n' for a, b, c in R_ROWS))
        cls.write('s.csv', ''.join(f'{b},{d}\n' for b, d in S_ROWS))
        cls.write('plan.json', PLAN)
        cls.write('q.sql', QUERY)
        # 20 pages requested in order, twice, through 16 frames: LRU never has the page it needs.
        cls.write('trace.txt', ''.join(f'{i}\n' for i in list(range(20)) * 2))
        for args in (['create-table', '--name', 'R', '--schema', 'a:INT,b:VARCHAR(20),c:INT'],
                     ['create-table', '--name', 'S', '--schema', 'b:VARCHAR(20),d:INT'],
                     ['load', '--table', 'R', '--file', 'r.csv'],
                     ['load', '--table', 'S', '--file', 's.csv']):
            r = cls.tool('storage', *args)
            assert r.returncode == 0, r.stdout + r.stderr

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def write(cls, name, text):
        with open(os.path.join(cls.dir, name), 'w') as f:
            f.write(text)

    @classmethod
    def tool(cls, name, *args):
        """Run one of the four tools from the directory that holds db/."""
        return subprocess.run([sys.executable, os.path.join(ROOT, name + '.py'), *args], cwd=cls.dir,
                              capture_output=True, text=True)

    def ok(self, name, *args):
        r = self.tool(name, *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_storage(self):
        self.assertTrue(os.path.isdir(os.path.join(self.dir, 'db')))        # create-table made ./db
        out = json.loads(self.ok('storage', 'scan', '--table', 'R', '--limit', '20', '--frames', '8', '--json'))
        self.assertEqual([tuple(row) for row in out['rows']], R_ROWS[:20])
        self.assertLessEqual({'hits', 'misses', 'evictions', 'reads', 'writes'}, set(out['stats']))
        self.assertIn('slot 0 at byte', self.ok('storage', 'layout', '--table', 'R', '--page', '3'))
        out = json.loads(self.ok('storage', 'trace', '--file', 'trace.txt', '--frames', '16', '--policy', 'lru',
                                 '--json'))
        self.assertEqual((out['hits'], out['misses'], out['evictions'], out['writes']), (0, 40, 24, 0))

    def test_exec(self):
        out = json.loads(self.ok('exec', 'run', '--plan', 'plan.json', '--frames', '32', '--json'))
        self.assertEqual(len(out['rows']), JOINED)
        self.assertEqual(out['totals']['tuples_out'], JOINED)
        lines = self.ok('exec', 'trace', '--plan', 'plan.json', '--frames', '32').splitlines()
        self.assertEqual([line.split()[0] for line in lines], ['hash_join', 'filter', 'seqscan', 'seqscan'])

    def test_sql(self):
        self.assertIn('where: r.a > 500', self.ok('sql', 'parse', '--query', QUERY, '--explain'))
        plan = json.loads(self.ok('sql', 'compile', '--file', 'q.sql', '--catalog', 'db/', '--json'))
        self.assertEqual(plan['op'], 'project')
        self.assertEqual(self.ok('sql', 'check', '--file', 'q.sql', '--catalog', 'db/'), '')

    def test_opt_and_the_whole_path(self):
        self.write('logical.json', self.ok('sql', 'compile', '--file', 'q.sql', '--catalog', 'db/', '--json'))
        for table in ('R', 'S'):
            self.assertIn(f'{table}: n_tuples=',
                          self.ok('opt', 'analyze', '--catalog', 'db/', '--table', table, '--buckets', '20'))
        physical = self.ok('opt', 'plan', '--logical', 'logical.json', '--catalog', 'db/', '--frames', '32', '--json')
        for line in self.ok('opt', 'explain', '--logical', 'logical.json', '--catalog', 'db/',
                            '--frames', '32').splitlines():
            self.assertIn('est_rows=', line)
            self.assertIn('est_cost=', line)
        table = self.ok('opt', 'dp-table', '--logical', 'logical.json', '--catalog', 'db/').splitlines()
        self.assertEqual([line.split()[0] for line in table[:4]], ['subset', '{r}', '{s}', '{r,s}'])
        self.write('physical.json', physical)
        out = json.loads(self.ok('exec', 'run', '--plan', 'physical.json', '--frames', '32', '--json'))
        self.assertEqual(len(out['rows']), JOINED)
