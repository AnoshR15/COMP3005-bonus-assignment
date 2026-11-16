"""The sql command line, run as a subprocess from the project root."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from tests.base import ROOT

DB = 'tests/data/sqldb'


class SqlCommandLineTests(unittest.TestCase):

    def sql(self, *args):
        return subprocess.run([sys.executable, 'sql.py', *args], cwd=ROOT, capture_output=True, text=True)

    def test_parse_explain_needs_no_catalog(self):
        r = self.sql('parse', '--query', 'SELECT city FROM Nowhere UNION ALL SELECT city FROM Dept', '--explain')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.splitlines()[0], 'union_all')
        self.assertIn('    table Nowhere', r.stdout)

    def test_compile_json_is_one_plan_object(self):
        r = self.sql('compile', '--db', DB, '--query', 'SELECT name, age FROM Emp WHERE age > 30;', '--json')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), {
            'op': 'project', 'columns': ['Emp.name', 'Emp.age'],
            'child': {'op': 'select', 'condition': 'Emp.age > 30', 'child': {'op': 'scan', 'table': 'Emp'}}})

    def test_compile_file_with_several_statements_and_explain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'q.sql')
            with open(path, 'w') as f:
                f.write('-- two queries\nSELECT city FROM Emp UNION SELECT city FROM Dept;\n'
                        'SELECT COUNT(*) FROM Emp;\n')
            r = self.sql('compile', '--db', DB, '--file', path, '--json')
            self.assertEqual([p['op'] for p in json.loads(r.stdout)], ['union', 'project'])
            r = self.sql('compile', '--catalog', DB, '--file', path, '--explain')
            self.assertEqual(r.stdout.splitlines()[:5], [
                'union', '  project ["Emp.city"]', '    scan Emp', '  project ["Dept.city"]', '    scan Dept'])

    def test_check_prints_only_errors_and_sets_the_exit_code(self):
        r = self.sql('check', '--db', DB, '--query', 'SELECT name FROM Emp')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        r = self.sql('check', '--db', DB, '--query', "SELECT name FROM Emp\n WHERE age > '30'")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(),
                         "error: type: cannot compare INT with VARCHAR in 'Emp.age > '30'' at line 2, column 12")

    def test_no_stack_traces(self):
        for args in [('check', '--db', DB, '--file', 'no_such.sql'),
                     ('check', '--db', 'no_such_dir', '--query', 'SELECT a FROM T'),
                     ('compile', '--db', DB, '--query', 'SELECT FROM'),
                     ('parse', '--query', "SELECT 'x")]:
            with self.subTest(args=args):
                r = self.sql(*args)
                self.assertEqual(r.returncode, 1)
                self.assertTrue(r.stdout.startswith('error:'), r.stdout)
                self.assertNotIn('Traceback', r.stdout + r.stderr)
