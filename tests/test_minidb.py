"""The integrated system: SQL text in at one end, rows out at the other, through all four modules."""
import os
import subprocess
import sys
import tempfile
import unittest
from tests.base import ROOT
from minidb import split_statements

SETUP = """
-- a small company
CREATE TABLE Dept (did INT, title VARCHAR(20), budget INT);
CREATE TABLE Emp (eid INT, name VARCHAR(20), age INT, did INT);
INSERT INTO Dept VALUES (1, 'Engineering', 5000);
INSERT INTO Dept VALUES (2, 'Sales; and more', 800);   -- a semicolon inside a string
INSERT INTO Dept VALUES (3, 'Law', 2000);
LOAD Emp FROM '{csv}';
ANALYZE;
"""


class MiniDbTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.db = os.path.join(self.dir, 'db')
        self.csv = os.path.join(self.dir, 'emp.csv')
        with open(self.csv, 'w') as f:
            for i in range(400):
                f.write(f'{i},emp{i},{20 + i % 40},{1 + i % 3}\n')
        self.run_script(SETUP.format(csv=self.csv))

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, text, *args):
        path = os.path.join(self.dir, 'script.sql')
        with open(path, 'w') as f:
            f.write(text)
        r = subprocess.run([sys.executable, 'minidb.py', '--db', self.db, '--file', path, *args], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertNotIn('Traceback', r.stderr)
        return r

    def test_setup_messages(self):
        r = self.run_script("CREATE TABLE T (a INT); INSERT INTO T VALUES (7); SELECT * FROM T;")
        self.assertEqual(r.stdout.splitlines()[:4], ['created table T', 'inserted 1 row', 'T.a', '7'])
        self.assertRegex(r.stdout.splitlines()[4], r'^\(1 row; page reads \d+, writes 0; \d+ ms; estimated rows 1\)$')

    def test_a_query_goes_through_all_four_modules(self):
        r = self.run_script("SELECT d.title, COUNT(*) AS n, AVG(e.age) AS years FROM Emp e JOIN Dept d "
                            "ON e.did = d.did WHERE e.age >= 30 GROUP BY d.title ORDER BY n DESC;")
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], 'd.title | n | years')
        expected = {}
        for i in range(400):
            if 20 + i % 40 >= 30:
                expected.setdefault(1 + i % 3, []).append(20 + i % 40)
        titles = {1: 'Engineering', 2: 'Sales; and more', 3: 'Law'}
        want = sorted(((len(v), titles[k], sum(v) / len(v)) for k, v in expected.items()), reverse=True)
        got = [line.split(' | ') for line in lines[1:4]]
        self.assertEqual([(int(n), t) for t, n, _ in got], [(n, t) for n, t, _ in want])
        self.assertEqual([y for _, _, y in got], [f'{avg:.4f}' for _, _, avg in want])    # four decimals
        self.assertRegex(lines[4], r'^\(3 rows; page reads \d+, writes \d+; \d+ ms; estimated rows 3\)$')

    def test_explain_and_explain_analyze(self):
        r = self.run_script("EXPLAIN SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did WHERE d.budget > 1000;")
        lines = r.stdout.splitlines()
        self.assertRegex(lines[0], r'^project \(e\.name\) +est_rows=\d+ +est_cost=0$')
        self.assertTrue(any('seqscan Emp' in line and 'est_rows=400' in line for line in lines))
        r = self.run_script("EXPLAIN ANALYZE SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did "
                            "WHERE d.budget > 1000;")
        lines = r.stdout.splitlines()
        scan = next(line for line in lines if 'seqscan Emp' in line)
        self.assertRegex(scan, r'est_rows=400 +actual_rows=400 +est_cost=(\d+) +actual_io=\1$')
        top = lines[0]
        self.assertRegex(top, r'^project \(e\.name\) +est_rows=\d+ +actual_rows=267 ')      # depts 1 and 3
        self.assertRegex(lines[-1], r'^\(267 rows; estimated cost \d+, page reads \d+, writes \d+; \d+ ms; '
                                    r'most frames in use \d+ of 32\)$')

    def test_set_frames_changes_the_budget(self):
        r = self.run_script("SET frames = 4; EXPLAIN ANALYZE SELECT name FROM Emp ORDER BY age;"
                            "SET frames=64; EXPLAIN ANALYZE SELECT name FROM Emp ORDER BY age;")
        out = r.stdout
        self.assertIn('frames = 4\n', out)
        self.assertIn('frames = 64\n', out)
        self.assertRegex(out, r'most frames in use [1-4] of 4\)')
        self.assertRegex(out, r'most frames in use \d+ of 64\)')
        self.assertIn('error: frames must be at least 3', self.run_script("SET frames = 2;").stdout)

    def test_errors_are_one_line_and_the_script_goes_on(self):
        r = self.run_script("SELECT nope FROM Emp;\nSELECT name FROM Emp WHERE age > 'x';\n"
                            "EXPLAIN SELECT name\n  FROM Nowhere;\nLOAD Emp FROM 'no_such.csv';\n"
                            "INSERT INTO Dept VALUES (1, 2, 3);\nANALYZE Nowhere;\nSELECT title FROM Dept WHERE did = 3;")
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "error: name: unknown column 'nope' at line 1, column 8")
        self.assertIn('error: type: cannot compare INT with VARCHAR', lines[1])
        self.assertEqual(lines[2], "error: name: unknown table 'Nowhere' at line 3, column 8")    # position kept
        self.assertTrue(lines[3].startswith('error: cannot read'))
        self.assertIn("error: type: column 'title' is VARCHAR(20)", lines[4])
        self.assertEqual(lines[5], "error: unknown table 'Nowhere'")
        self.assertEqual(lines[6:8], ['Dept.title', 'Law'])                 # and the last statement still ran
        self.assertEqual(r.returncode, 1)

    def test_report_mode(self):
        r = self.run_script("SELECT name FROM Emp WHERE age = 25;\n"
                            "SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did WHERE d.budget > 1000;\n"
                            "SELECT oops FROM Emp;\nSELECT did, COUNT(*) AS n FROM Emp GROUP BY did;", '--report')
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0].split(), ['#', 'rows', 'reads', 'writes', 'ms', 'est', 'rows', 'actual', 'q-error',
                                            'est', 'cost'])
        first = lines[1].split()
        self.assertEqual((first[0], first[1], first[5], first[6], first[7]), ('1', '10', '10', '10', '1.00'))
        self.assertEqual(int(first[8]), int(first[2]) + int(first[3]))      # one scan: predicted is what was read
        self.assertEqual(lines[2].split()[1], '267')
        self.assertIn("error: name: unknown column 'oops'", lines[3])
        self.assertEqual(lines[4].split()[1], '3')

    def test_the_module_tools_still_work_on_the_same_database(self):
        r = subprocess.run([sys.executable, 'storage.py', 'scan', '--db', self.db, '--table', 'Dept', '--json'],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertIn('"rows": [[1, "Engineering", 5000], [2, "Sales; and more", 800], [3, "Law", 2000]]', r.stdout)
        r = subprocess.run([sys.executable, 'opt.py', 'explain', '--db', self.db, '--frames', '8', '--sql',
                            'SELECT name FROM Emp WHERE age = 25'], cwd=ROOT, capture_output=True, text=True)
        self.assertRegex(r.stdout, r'filter \(Emp\.age = 25\) +est_rows=10 ')

    def test_interactive_prompt(self):
        r = subprocess.run([sys.executable, 'minidb.py', '--db', self.db], cwd=ROOT, capture_output=True, text=True,
                           input="SELECT title\n  FROM Dept\n WHERE did = 1;\nSELECT COUNT(*) FROM Emp; quit\n")
        self.assertIn('Engineering', r.stdout)
        self.assertIn('\n400\n', r.stdout)

    def test_a_sum_larger_than_an_int_survives_a_temporary_file(self):
        """Three values of two billion: their sum does not fit in 4 bytes. Printing it was never
        the problem; writing it to a temporary file was, and at B = 4 the join and the DISTINCT
        below do that. Both used to end in a stack trace. The sort and the UNION keep two rows
        in memory and always worked; they are here so that all four stay right."""
        sub = "(SELECT g, SUM(a) AS s FROM Wide GROUP BY g) AS t"
        r = self.run_script(
            "SET frames = 4;"
            "CREATE TABLE Wide (g INT, a INT); CREATE TABLE K (g INT);"
            "INSERT INTO Wide VALUES (1, 2000000000); INSERT INTO Wide VALUES (1, 2000000000);"
            "INSERT INTO Wide VALUES (2, 2000000000); INSERT INTO K VALUES (1); INSERT INTO K VALUES (2);"
            f"SELECT k.g, t.s FROM K k JOIN {sub} ON k.g = t.g;"
            f"SELECT t.s FROM {sub} ORDER BY t.s DESC;"
            f"SELECT DISTINCT t.s FROM {sub};"
            f"SELECT t.s FROM {sub} UNION SELECT g FROM K;")
        self.assertEqual(r.returncode, 0, r.stdout)
        lines = r.stdout.splitlines()
        self.assertIn('1 | 4000000000', lines)                          # through the join
        self.assertIn('2 | 2000000000', lines)
        self.assertEqual(lines.count('4000000000'), 3)                  # the sort, the DISTINCT, the UNION
        self.assertEqual(lines[lines.index('t.s') + 1:lines.index('t.s') + 3], ['4000000000', '2000000000'])

    def test_splitting_statements(self):
        self.assertEqual(split_statements("a; b 'x;y' ; -- c;\n d;;"), ['a', " b 'x;y' ", ' -- c;\n d'])
        self.assertEqual(split_statements("a 'it''s; fine'; b"), ["a 'it''s; fine'", ' b'])
        self.assertEqual(split_statements(' \n -- nothing\n'), [])
