"""The command line, run as a subprocess from the project root."""
import subprocess
import sys
import unittest
from tests.base import ROOT


class CommandLineTests(unittest.TestCase):

    def ra(self, *args):
        return subprocess.run([sys.executable, 'ra.py', *args], cwd=ROOT, capture_output=True, text=True)

    def test_tree_matches_the_assignment_sample(self):
        r = self.ra('--tree', 'project[Name](select[Age>30](Employees))')
        self.assertEqual(r.stdout.strip(),
                         'Project(attrs=[Name])\n└── Select(cond=Gt(Attr(Age), Num(30)))\n    └── Relation(Employees)')

    def test_missing_file_is_a_message_not_a_trace(self):
        r = self.ra('--run', 'no_such_file.ra', 'Employees')
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith('Error: cannot read'))
        self.assertNotIn('Traceback', r.stderr)

    def test_every_error_category_is_one_line(self):
        for q in ["select[Name='Bob](Employees)",          # lexical
                  'select[Age>30](Employees',              # syntax
                  'Missing',                               # name
                  'Employees union Departments',           # schema
                  "select[Age>'30'](Employees)"]:          # type
            with self.subTest(query=q):
                r = self.ra('--run', 'data/example.ra', q)
                self.assertEqual(r.returncode, 1)
                self.assertTrue(r.stdout.startswith('Error:'))
                self.assertNotIn('Traceback', r.stderr)

    def test_empty_result_prints_only_the_schema(self):
        r = self.ra('--run', 'data/example.ra', 'select[Age>100](Employees)')
        self.assertEqual(r.stdout.strip(), 'Schema: Employees.EID, Employees.Name, Employees.Age, Employees.DID')
        self.assertIn('(0 tuples)', r.stderr)
        self.assertIn('join compared 0 pairs', r.stderr)
