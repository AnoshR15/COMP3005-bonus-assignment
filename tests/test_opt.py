"""The query optimizer: statistics, selectivity, rewrite rules, cost model, join enumeration.

The checklist of Section 8 of the Module 4 specification is the class ChecklistTests."""
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from tests.base import ROOT
from tests.optbase import OptTestCase, TABLES, bag
from executor.build import validate
from optimizer import cost as C
from optimizer.estimate import ColEst, Est, filtered, selectivity, two_columns, round_rows, pages_for
from optimizer.planner import Planner, naive
from optimizer.rewrite import rewrite, tree_text
from optimizer.stats import analyze, build_histogram
from source.condition import parse_condition
from store.db import Database

COMMA = ("SELECT e.name FROM Emp e, Dept d "
         "WHERE e.did = d.did AND e.age > 60 AND d.budget > 1000")
JOIN_ON = ("SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did "
           "WHERE e.age > 60 AND d.budget > 1000")

# Queries for the end-to-end checks: every clause, every logical operator.
QUERIES = [
    "SELECT name, age FROM Emp WHERE age > 60 AND did = 3",
    COMMA,
    JOIN_ON,
    "SELECT e.name, d.title, p.pname FROM Emp e, Dept d, Proj p WHERE e.did = d.did AND p.did = d.did AND e.age < 25",
    "SELECT e.name, p.pname, w.hours FROM Works w JOIN Emp e ON w.eid = e.eid JOIN Proj p ON w.pid = p.pid "
    "JOIN Dept d ON p.did = d.did WHERE d.budget > 1500 AND w.hours > 30",
    "SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid WHERE e.age = 33",
    "SELECT did, COUNT(*) AS n, AVG(salary) AS pay FROM Emp WHERE age > 30 GROUP BY did HAVING COUNT(*) > 10 "
    "ORDER BY n DESC LIMIT 5",
    "SELECT d.title, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t JOIN Dept d ON t.did = d.did "
    "WHERE t.n > 20 AND d.budget < 1000",
    "SELECT DISTINCT city FROM Emp WHERE salary > 4000",
    "SELECT city FROM Emp WHERE age > 65 UNION SELECT city FROM Dept WHERE budget > 1700",
    "SELECT did FROM Emp WHERE age < 22 UNION ALL SELECT did FROM Proj",
    "SELECT did FROM Emp INTERSECT SELECT did FROM Proj EXCEPT SELECT did FROM Dept WHERE budget > 900",
    "SELECT name FROM Emp WHERE salary > 4800 ORDER BY age DESC LIMIT 3",
    "SELECT COUNT(*) FROM Emp e JOIN Works w ON e.eid = w.eid WHERE e.city = 'city3'",
    "SELECT e.name, w.hours FROM Emp e JOIN Works w ON e.eid = w.eid AND w.hours > e.age WHERE e.did = 4",
    "SELECT e.name, d.title FROM Emp e, Dept d WHERE e.salary < d.budget AND e.eid < 5 AND d.did < 4",
    "SELECT a.x, c.y FROM A a, C c WHERE a.x < 2",
    "SELECT e.eid FROM Emp e JOIN Empty x ON e.eid = x.a",
    "SELECT * FROM Dept d JOIN Proj p ON d.did = p.did WHERE p.pid < 3",
]


def join_nodes(plan):
    """Every join of a physical plan, as (operator, left input, right input)."""
    found = []

    def walk(node):
        if node['op'].endswith('_join'):
            found.append(node)
        for field in ('child', 'left', 'right'):
            if field in node:
                walk(node[field])
    walk(plan)
    return found


def ops(plan):
    out = [plan['op']]
    for field in ('child', 'left', 'right'):
        if field in plan:
            out += ops(plan[field])
    return out


class ChecklistTests(OptTestCase):

    def test_the_rewrite_example_reproduces(self):
        """Section 3: the comma join, before and after."""
        result = self.optimize(COMMA)
        self.assertEqual(tree_text(result.before).splitlines(), [
            'project [e.name]',
            '  select e.did = d.did and e.age > 60 and d.budget > 1000',
            '    cross',
            '      rename e (scan Emp)',
            '      rename d (scan Dept)'])
        self.assertEqual(tree_text(result.after).splitlines(), [
            'project [e.name]',
            '  join e.did = d.did',
            '    project [e.name, e.did]',
            '      select e.age > 60',
            '        rename e (scan Emp)',
            '    project [d.did]',
            '      select d.budget > 1000',
            '        rename d (scan Dept)'])

    def test_a_pushed_projection_lists_what_is_needed_above_first(self):
        """project [e.name, e.did] in the example is e.name for the select list above, then e.did
        for the join: the order of rule 6's own sentence. In Emp, name happens to come before
        did, so the table's column order would give the same line. In Proj, did comes before
        pname, and the line must still start with the column needed above."""
        result = self.optimize("SELECT p.pname FROM Proj p, Dept d WHERE p.did = d.did AND d.budget > 1000")
        self.assertEqual(tree_text(result.after).splitlines(), [
            'project [p.pname]',
            '  join p.did = d.did',
            '    project [p.pname, p.did]',
            '      rename p (scan Proj)',
            '    project [d.did]',
            '      select d.budget > 1000',
            '        rename d (scan Dept)'])

    def test_the_dp_table_of_section_5_1_reproduces(self):
        """A is 100 pages and 5,000 tuples, B' 10 and 500, C 400 and 20,000; A.x = B'.x gives 1,000
        rows on 20 pages, B'.y = C.y gives 2,000 on 40; nothing joins A and C. B = 32."""
        logical = {'op': 'join', 'condition': 'Bp.y = C.y',
                   'left': {'op': 'join', 'condition': 'A.x = Bp.x',
                            'left': {'op': 'scan', 'table': 'A'}, 'right': {'op': 'scan', 'table': 'Bp'}},
                   'right': {'op': 'scan', 'table': 'C'}}
        assume = {'rows': {'A': 5000, 'Bp': 500, 'C': 20000, 'A,Bp': 1000, 'Bp,C': 2000, 'A,Bp,C': 4000},
                  'pages': {'A': 100, 'Bp': 10, 'C': 400, 'A,Bp': 20, 'Bp,C': 40}}
        result = self.optimize(logical, frames=32, assume=assume)
        table = {e['subset']: e for e in result.tables[0]}
        self.assertEqual([(s, table[s]['cost']) for s in ('{A}', '{Bp}', '{C}')],
                         [('{A}', 100), ('{Bp}', 10), ('{C}', 400)])
        self.assertEqual(table['{A,Bp}']['plan'], '{Bp} outer, nested loop with A inner: 10 + 1 x 100')
        self.assertEqual(table['{A,Bp}']['cost'], 110)
        self.assertEqual(table['{Bp,C}']['plan'], '{Bp} outer, nested loop with C inner: 10 + 1 x 400')
        self.assertEqual(table['{Bp,C}']['cost'], 410)
        self.assertEqual(table['{A,C}']['plan'], 'no join predicate, skipped')
        self.assertEqual(table['{A,Bp,C}']['plan'], '{A,Bp} outer, nested loop with C inner: 110 + 20 + 1 x 400')
        self.assertEqual(table['{A,Bp,C}']['cost'], 530)
        # The other way round: 40 pages of outer do not fit in the 30 frames of a block, so A is
        # read twice. (The specification's table writes this row as 410 + 40 + 100 = 550.)
        self.assertEqual(table['{A,Bp,C}']['alternatives'][0],
                         ('{Bp,C} outer, nested loop with A inner: 410 + 40 + 2 x 100', 650))
        plan = result.plan.to_json()
        # The joins come out as (Bp, A), C. The query wrote A first, and nothing above names the
        # columns, so a projection puts them back in the written order.
        self.assertEqual((plan['op'], plan['columns']), ('project', ['A.x', 'A.y', 'Bp.x', 'Bp.y', 'C.x', 'C.y']))
        plan = plan['child']
        self.assertEqual((plan['op'], plan['right']['table']), ('nested_loop_join', 'C'))
        self.assertEqual((plan['left']['op'], plan['left']['left']['table'], plan['left']['right']['table']),
                         ('nested_loop_join', 'Bp', 'A'))
        self.assertRegex(result.dp_text(), r'\{A,C\} +no join predicate, skipped\n')

    def test_every_physical_plan_runs_unedited_in_module_2(self):
        for sql in QUERIES:
            logical = self.logical(sql)
            expected = bag(self.reference(logical))
            for frames in (4, 8, 32):
                with self.subTest(sql=sql, frames=frames):
                    physical = self.optimize(logical, frames).plan.to_json()
                    validate(json.loads(json.dumps(physical)))          # exactly what `opt plan` prints
                    rows, _ = self.execute(physical, frames)
                    self.assertEqual(bag(rows), expected)

    def test_commas_and_join_on_give_the_same_physical_plan(self):
        pairs = [(COMMA, JOIN_ON),
                 ("SELECT e.name, p.pname FROM Emp e, Dept d, Proj p WHERE e.did = d.did AND p.did = d.did AND e.age < 30",
                  "SELECT e.name, p.pname FROM Emp e JOIN Dept d ON e.did = d.did JOIN Proj p ON p.did = d.did "
                  "WHERE e.age < 30"),
                 ("SELECT w.hours FROM Works w, Emp e, Proj p, Dept d WHERE w.eid = e.eid AND w.pid = p.pid "
                  "AND p.did = d.did AND d.budget > 500",
                  "SELECT w.hours FROM Works w JOIN Emp e ON e.eid = w.eid JOIN Proj p ON w.pid = p.pid "
                  "JOIN Dept d ON d.did = p.did WHERE d.budget > 500")]
        for commas, join_on in pairs:
            for frames in (4, 16):
                with self.subTest(sql=commas, frames=frames):
                    a = self.optimize(commas, frames).plan.to_json()
                    b = self.optimize(join_on, frames).plan.to_json()
                    self.assertEqual(a, b)
                    self.assertGreaterEqual(len(join_nodes(a)), 1)

    def test_changing_b_changes_the_join_algorithm(self):
        sql = "SELECT * FROM W1 a JOIN W2 b ON a.k = b.k"       # 30 pages and 20 pages
        chosen = {frames: join_nodes(self.optimize(sql, frames).plan.to_json())[0]['op'] for frames in (6, 8, 64)}
        self.assertEqual(chosen[64], 'nested_loop_join')        # the outer fits in one block: M + N = 50
        self.assertEqual(chosen[8], 'nested_loop_join')         # 30 + 5 x 20 = 130 against 3 x 50 = 150
        self.assertEqual(chosen[6], 'hash_join')                # 20 + 5 x 30 = 170 against 150

    def test_running_analyze_twice_changes_nothing(self):
        path = os.path.join(self.dir, 'catalog.db')
        with open(path, 'rb') as f:
            before = f.read()
        db = Database(self.dir, 16)
        first = db.catalog.get_table('Emp').stats
        for table in ('Emp', 'Dept', 'Skew'):
            analyze(db, table)
        again = db.catalog.get_table('Emp').stats
        db.close()
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), before)
        again.pop('io', None)
        self.assertEqual(again, first)

    def test_a_plan_without_statistics_is_still_valid(self):
        """A, Bp and C were never analyzed: row and page counts come from the catalog, the rest
        from the documented defaults."""
        sql = "SELECT a.x, c.y FROM A a JOIN Bp b ON a.x = b.x JOIN C c ON b.y = c.y WHERE a.y > 1 AND c.x = 3"
        result = self.optimize(sql)
        rows, _ = self.execute(result.plan.to_json())
        self.assertEqual(bag(rows), bag(self.reference(self.logical(sql))))
        estimates = {node.label(): node.rows for _, node in result.plan.walk()}
        self.assertEqual(estimates['filter (a.y > 1)'], 2)             # ceil(5 * 1/3)
        self.assertEqual(estimates['filter (c.x = 3)'], 1)             # ceil(5 * 0.1)


class StatisticsTests(OptTestCase):

    def stats(self, table):
        db = Database(self.dir, 8)
        try:
            return db.catalog.get_table(table).stats
        finally:
            db.disk.close()

    def test_table_and_column_statistics_are_exact(self):
        s = self.stats('Emp')
        columns, rows = TABLES['Emp']
        self.assertEqual((s['n_tuples'], s['buckets']), (600, 20))
        db = Database(self.dir, 8)
        info = db.catalog.get_table('Emp')
        self.assertEqual(s['n_pages'], info.num_pages)
        codec = info.codec()
        db.disk.close()
        self.assertEqual(s['width'], round(sum(len(codec.encode(r)) for r in rows) / 600, 2))
        for i, (name, kind, _) in enumerate(columns):
            values = [r[i] for r in rows]
            c = s['columns'][name]
            self.assertEqual((c['ndv'], c['min'], c['max']), (len(set(values)), min(values), max(values)), name)
            self.assertEqual(sum(b[2] for b in c['hist']), 600)                 # every tuple in one bucket
            self.assertEqual(sum(b[3] for b in c['hist']), c['ndv'])
        self.assertEqual(s['columns']['age']['width'], 4)
        self.assertEqual(s['columns']['name']['width'], round(sum(2 + len(r[1]) for r in rows) / 600, 2))

    def test_the_histogram_is_equi_depth(self):
        hist = self.stats('Emp')['columns']['eid']['hist']                      # 600 different values
        self.assertEqual(len(hist), 20)
        self.assertEqual([b[2] for b in hist], [30] * 20)
        self.assertEqual(hist[0][:2], [0, 29])
        self.assertEqual(hist[-1][:2], [570, 599])
        salary = self.stats('Emp')['columns']['salary']['hist']
        for a, b in zip(salary, salary[1:]):
            self.assertLess(a[1], b[0])                                         # no value in two buckets
        self.assertTrue(all(25 <= b[2] <= 40 for b in salary[:-1]), [b[2] for b in salary])

    def test_a_common_value_gets_a_bucket_to_itself(self):
        hist = self.stats('Skew')['columns']['status']['hist']
        self.assertEqual(hist[0], [0, 0, 700, 1])                               # 70 percent of the rows
        self.assertEqual([b[:2] for b in hist[1:]], [[7, 7], [8, 8], [9, 9]])
        ndv, low, high, built = build_histogram([(1, 5), (2, 100), (3, 5), (4, 5)], 115, 4)
        self.assertEqual((ndv, low, high), (4, 1, 4))
        self.assertEqual(built, [[1, 1, 5, 1], [2, 2, 100, 1], [3, 4, 10, 2]])

    def test_analyze_sorts_with_the_external_sort_inside_the_frames(self):
        db = Database(self.dir, 16)
        heap = db.create_table('Wide', [('k', 'INT', None), ('pad', 'VARCHAR', 200)])
        for i in range(19 * 12):
            heap.insert(((i * 37) % 228, f'{i:04d}' + 'x' * 196))
        db.close()
        db = Database(self.dir, 4)                      # 12 pages of pad cannot be sorted in 4 frames at once
        stats = analyze(db, 'Wide')
        self.assertEqual(db.pool.high_water, 4)
        self.assertEqual((db.pool.pinned, len(db.pool.reserved), db.disk.live_temps), (0, 0, 0))
        db.close()
        self.assertEqual(stats['columns']['k']['ndv'], 228)
        self.assertEqual(stats['columns']['pad']['min'], '0000' + 'x' * 196)
        self.assertGreater(stats['io']['page_writes'], 2 * 12)      # the column file, then runs, then merges
        self.assertEqual(stats['io']['page_reads'] - stats['io']['page_writes'], 12)    # one scan of the table
        db = Database(self.dir, 3)                      # two columns per scan would need 3 frames: fine
        self.assertEqual(analyze(db, 'Wide')['columns'], stats['columns'])
        db.close()
        db = Database(self.dir, 3)                      # 7 columns, 2 at a time: four scans, same answer
        emp = analyze(db, 'Emp')
        db.close()
        self.assertEqual(emp['columns'], self.stats('Emp')['columns'])

    def test_an_empty_table(self):
        s = self.stats('Empty')
        self.assertEqual((s['n_tuples'], s['width']), (0, 0))
        self.assertEqual(s['columns']['a'], {'ndv': 0, 'min': None, 'max': None, 'width': 0, 'hist': []})


class SelectivityTests(OptTestCase):

    def emp(self):
        """The worked example of Section 2: 10,000 tuples, ndv(did) = 50, age from 20 to 70."""
        return Est(10000, {'Emp.did': ColEst('INT', 4, 50, 0, 49), 'Emp.age': ColEst('INT', 4, 51, 20, 70)})

    def rows(self, est, condition):
        return filtered(est, parse_condition(condition)).rows

    def test_the_worked_example_of_section_2(self):
        emp = self.emp()
        self.assertEqual(self.rows(emp, 'Emp.did = 7'), 200)                    # 1/50
        self.assertEqual(self.rows(emp, 'Emp.age > 60'), 2000)                  # 10/50
        self.assertEqual(self.rows(emp, 'Emp.did = 7 and Emp.age > 60'), 40)    # 0.004
        dept = Est(50, {'Dept.did': ColEst('INT', 4, 50, 0, 49)})
        join = parse_condition('Emp.did = Dept.did')
        self.assertEqual(round_rows(10000 * 50 * two_columns(emp, dept, join)), 10000)

    def test_the_worked_example_as_real_data_with_one_bucket_and_with_twenty(self):
        """10,000 employees, 50 departments, ages 20 to 70, loaded and analyzed.

        With one bucket the estimate is the specification's arithmetic, (70 - 60) / (70 - 20), and
        the four numbers of its example come out. With twenty buckets the linear fraction is
        taken inside a bucket of three ages, 59 to 61, and calls half of it above 60 where one
        age of three is: 2,058 for a true 1,960. A finer histogram and a worse estimate."""
        ages = [20 + (i * 7) % 51 for i in range(10000)]
        self.assertEqual(sum(1 for age in ages if age > 60), 1960)
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory, 16)
            emp = db.create_table('Emp', [('eid', 'INT', None), ('did', 'INT', None), ('age', 'INT', None)])
            for i, age in enumerate(ages):
                emp.insert((i, i % 50, age))
            dept = db.create_table('Dept', [('did', 'INT', None)])
            for i in range(50):
                dept.insert((i,))
            db.flush()
            scan = {'op': 'scan', 'table': 'Emp'}

            def rows(logical, kind):
                plan = Planner(db.catalog, 32).optimize(logical).plan
                return next(n.rows for _, n in plan.walk() if n.op.endswith(kind))

            def kept(condition):
                return rows({'op': 'select', 'condition': condition, 'child': scan}, 'filter')

            got = {}
            for buckets in (1, 20):
                analyze(db, 'Emp', buckets)
                analyze(db, 'Dept', buckets)
                joined = {'op': 'join', 'condition': 'Emp.did = Dept.did', 'left': scan,
                          'right': {'op': 'scan', 'table': 'Dept'}}
                got[buckets] = (kept('Emp.did = 7'), kept('Emp.age > 60'), kept('Emp.did = 7 and Emp.age > 60'),
                                rows(joined, '_join'))
            db.close()
        self.assertEqual(got[1], (200, 2000, 40, 10000))
        self.assertEqual(got[20], (200, 2058, 42, 10000))

    def test_or_not_and_not_equal(self):
        emp = self.emp()
        p, q = 1 / 50, 10 / 50
        self.assertAlmostEqual(selectivity(emp, parse_condition('Emp.did = 7 or Emp.age > 60')), p + q - p * q)
        self.assertAlmostEqual(selectivity(emp, parse_condition('not Emp.did = 7')), 1 - p)
        self.assertAlmostEqual(selectivity(emp, parse_condition('Emp.did <> 7')), 1 - p)
        self.assertAlmostEqual(selectivity(emp, parse_condition('60 < Emp.age')), q)          # constant on the left
        self.assertAlmostEqual(selectivity(emp, parse_condition('Emp.did = 99')), 0)          # outside min..max
        self.assertEqual(self.rows(emp, 'Emp.did = 99'), 1)                     # never below one row
        self.assertAlmostEqual(selectivity(emp, parse_condition('Emp.did < Emp.age')), 0.1)   # two columns

    def test_two_ranges_on_one_column_are_an_interval_not_a_product(self):
        column = ColEst('INT', 4, 100, 0, 99, [[i * 10, i * 10 + 9, 100, 10] for i in range(10)])
        est = Est(1000, {'T.a': column})
        self.assertEqual(self.rows(est, 'T.a > 10 and T.a < 20'), 100)          # about the bucket 10..19
        product = selectivity(est, parse_condition('T.a > 10 and T.a < 20'))
        self.assertAlmostEqual(product, 0.889 * 0.2, places=2)                  # what plain multiplying says
        self.assertEqual(self.rows(est, 'T.a > 50 and T.a < 30'), 1)            # an empty interval

    def test_estimates_against_the_real_data(self):
        cases = [("SELECT eid FROM Emp WHERE age > 60", 0.1), ("SELECT eid FROM Emp WHERE did = 7", 0.01),
                 ("SELECT eid FROM Emp WHERE salary < 1000", 0.1), ("SELECT eid FROM Emp WHERE city = 'city4'", 0.01),
                 ("SELECT k FROM Skew WHERE status = 0", 0.0), ("SELECT k FROM Skew WHERE status = 8", 0.0),
                 ("SELECT k FROM Skew WHERE status > 0", 0.0), ("SELECT eid FROM Emp WHERE age >= 30 AND age < 40", 0.15),
                 ("SELECT e.eid FROM Emp e JOIN Dept d ON e.did = d.did", 0.0),
                 ("SELECT e.eid FROM Emp e JOIN Works w ON e.eid = w.eid", 0.01)]
        for sql, tolerance in cases:
            with self.subTest(sql=sql):
                logical = self.logical(sql)
                actual = len(self.reference(logical))
                estimate = self.optimize(logical).plan.rows
                self.assertLessEqual(abs(estimate - actual), tolerance * actual + 1, (estimate, actual))

    def test_defaults_without_statistics(self):
        est = Est(900, {'T.a': ColEst.default('INT', None), 'T.s': ColEst.default('VARCHAR', 20)})
        self.assertEqual(self.rows(est, 'T.a = 5'), 90)                         # 0.1
        self.assertEqual(self.rows(est, 'T.a > 5'), 300)                        # 1/3
        self.assertEqual(est.cols['T.s'].width, 12)                             # 2 + 20 / 2
        self.assertEqual(two_columns(est, est, parse_condition('T.a = T.a')), 0.1)

    def test_pages(self):
        self.assertEqual(pages_for(255, 12), 1)         # what fits on a page of Module 1
        self.assertEqual(pages_for(256, 12), 2)
        self.assertEqual(pages_for(0, 12), 1)


class RewriteTests(OptTestCase):

    def rewritten(self, sql, disabled=()):
        logical = self.logical(sql) if isinstance(sql, str) else sql
        db = Database(self.dir, 8)
        try:
            return rewrite(logical, db.catalog, disabled)
        finally:
            db.disk.close()

    def text(self, sql, disabled=()):
        return tree_text(self.rewritten(sql, disabled)).splitlines()

    def test_rule_1_is_what_lets_rule_2_and_3_work(self):
        without = self.text(COMMA, disabled={1})
        self.assertEqual(without[1], '  select e.did = d.did and e.age > 60 and d.budget > 1000')
        self.assertEqual(without[2].strip(), 'cross')                           # nothing could move

    def test_rule_2_pushes_only_what_one_side_can_answer(self):
        lines = self.text("SELECT e.name FROM Emp e JOIN Dept d ON e.did = d.did "
                          "WHERE e.age > 60 AND (e.salary > d.budget OR d.budget < 5)")
        self.assertEqual(lines[1], '  select e.salary > d.budget or d.budget < 5')      # both sides: stays above
        self.assertEqual(lines[2], '    join e.did = d.did')
        self.assertIn('        select e.age > 60', lines)
        off = self.text(JOIN_ON, disabled={2})
        self.assertEqual(off[1:3], ['  select e.age > 60 and d.budget > 1000', '    join e.did = d.did'])

    def test_rule_3_needs_an_equality_between_the_two_sides(self):
        lines = self.text("SELECT e.name FROM Emp e, Dept d WHERE e.salary < d.budget")
        self.assertEqual(lines[1:3], ['  select e.salary < d.budget', '    cross'])
        lines = self.text("SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did AND e.city = d.city")
        self.assertEqual(lines[1], '  join e.did = d.did and e.city = d.city')
        self.assertEqual(self.text(COMMA, disabled={3})[1:3], ['  select e.did = d.did', '    cross'])
        lines = self.text("SELECT e.name FROM Emp e, Dept d WHERE e.did = e.mgr")       # equality, one side only
        self.assertNotIn('join', ' '.join(lines))

    def test_rule_4_rewrites_names_through_a_rename_and_into_both_sides_of_a_union(self):
        sql = "SELECT t.did FROM (SELECT did, age AS years FROM Emp) AS t WHERE t.years > 60"
        self.assertEqual(self.text(sql, disabled={6, 7}), [         # t.years -> years -> Emp.age
            'project [t.did]', '  rename t', '    project [Emp.did, Emp.age as years]', '      select Emp.age > 60',
            '        scan Emp'])
        self.assertEqual(self.text(sql, disabled={4, 6, 7})[1:3], ['  select t.years > 60', '    rename t'])
        self.assertEqual(self.text("SELECT e.name FROM Emp e WHERE e.age > 60", disabled={6, 7}), [
            'project [e.name]', '  select e.age > 60', '    rename e (scan Emp)'])      # over a scan: left alone
        union = {'op': 'select', 'condition': 'Emp.did > 5',
                 'child': {'op': 'union_all',
                           'left': {'op': 'project', 'columns': ['Emp.did'], 'child': {'op': 'scan', 'table': 'Emp'}},
                           'right': {'op': 'project', 'columns': ['Proj.pid'], 'child': {'op': 'scan', 'table': 'Proj'}}}}
        self.assertEqual(self.text(union, disabled={6, 7}), [       # the right branch: Emp.did is Proj.pid there
            'union_all', '  project [Emp.did]', '    select Emp.did > 5', '      scan Emp',
            '  project [Proj.pid]', '    select Proj.pid > 5', '      scan Proj'])
        self.assertEqual(bag(self.reference(self.rewritten(union))), bag(self.reference(union)))

    def test_rule_5_moves_only_predicates_on_grouping_columns(self):
        sql = ("SELECT t.did, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t "
               "WHERE t.did < 5 AND t.n > 20")
        lines = self.text(sql, disabled={6, 7})
        self.assertEqual(lines, ['project [t.did, t.n]', '  rename t', '    project [Emp.did, n]',
                                 '      select n > 20',                     # on the aggregate: stays above
                                 '        group_by [Emp.did] count(*) as n',
                                 '          select Emp.did < 5',            # on the grouping column: below
                                 '            scan Emp'])
        having = self.logical("SELECT did, COUNT(*) AS n FROM Emp GROUP BY did HAVING COUNT(*) > 20")
        self.assertEqual(self.text(having)[:2], ['select n > 20', '  group_by [Emp.did] count(*) as n'])  # never moves
        grouped = {'op': 'select', 'condition': 'Emp.did < 5',
                   'child': {'op': 'group_by', 'keys': ['Emp.did'], 'aggregates': [['count', '*', 'n']],
                             'child': {'op': 'scan', 'table': 'Emp'}}}
        self.assertEqual(self.text(grouped, disabled={6})[:2], ['group_by [Emp.did] count(*) as n',
                                                                '  select Emp.did < 5'])
        self.assertEqual(self.text(grouped, disabled={5, 6})[0], 'select Emp.did < 5')

    def test_rule_6_never_drops_a_column_that_is_still_referenced(self):
        lines = self.text("SELECT e.name FROM Emp e JOIN Works w ON e.eid = w.eid WHERE w.hours > e.age")
        lines = [line.strip() for line in lines]
        self.assertEqual(lines[1:3], ['select w.hours > e.age', 'join e.eid = w.eid'])
        # what is needed above comes first: name for the select list, age and hours for the select
        # above the join; then eid, which the join itself needs
        self.assertIn('project [e.name, e.age, e.eid]', lines)
        self.assertIn('project [w.hours, w.eid]', lines)
        self.assertNotIn('project', ' '.join(self.text(JOIN_ON, disabled={6})[1:]))
        lines = [line.strip() for line in self.text("SELECT did, COUNT(*) AS n FROM Emp GROUP BY did ORDER BY n")]
        self.assertIn('project [Emp.did]', lines)                               # below the group_by
        lines = [line.strip() for line in self.text("SELECT COUNT(*) FROM Emp")]
        self.assertIn('project [Emp.eid]', lines)                               # one column must remain

    def test_rule_7_tidies_up(self):
        lines = self.text("SELECT * FROM Dept WHERE budget > 500 AND did < 9")
        self.assertEqual(lines, ['select Dept.budget > 500 and Dept.did < 9', '  scan Dept'])
        self.assertEqual(self.text("SELECT * FROM Dept WHERE budget > 500 AND did < 9", disabled={7}), [
            'project [Dept.did, Dept.title, Dept.budget, Dept.city]', '  select Dept.budget > 500',
            '    select Dept.did < 9', '      scan Dept'])

    def test_every_rewrite_means_the_same_query(self):
        for sql in QUERIES:
            logical = self.logical(sql)
            expected = bag(self.reference(logical))
            for disabled in [(), {1}, {2}, {3}, {4}, {5}, {6}, {7}, {1, 2, 3, 4, 5, 6, 7}]:
                with self.subTest(sql=sql, disabled=sorted(disabled)):
                    self.assertEqual(bag(self.reference(self.rewritten(logical, disabled))), expected)

    def test_rewriting_twice_changes_nothing_more(self):
        for sql in QUERIES:
            once = self.rewritten(sql)
            self.assertEqual(self.rewritten(once), once, sql)


class CostTests(unittest.TestCase):

    def test_the_formulas(self):
        self.assertEqual([C.sort_passes(m, b) for m, b in [(8, 3), (100, 32), (10, 32), (80, 4), (1000, 4)]],
                         [3, 2, 1, 4, 7])
        self.assertEqual(C.sort_cost(100, 32), 2 * 100 * 1 + 100)
        self.assertEqual(C.sort_cost(10, 32), 10)
        self.assertEqual(C.nested_loop_cost(10, 100, 32), 110)
        self.assertEqual(C.nested_loop_cost(40, 100, 32), 240)
        self.assertEqual(C.nested_loop_cost(120, 80, 4), 120 + 60 * 80)
        self.assertEqual(C.hash_cost(10, 100, 32), 330)
        self.assertEqual(C.sort_merge_cost(10, 100, 32), 10 + 300 + 110 + 110)

    def test_the_two_formulas_that_follow_the_executor(self):
        """REPORT_M2.md: at B = 16, M = N = 240, the hash join overflowed and cost 2,992, and
        sort-merge cost 2,400. 3 (M + N) would have said 1,440."""
        self.assertEqual(C.hash_levels(225, 16), 1)             # (B - 1)^2 pages fit one level
        self.assertEqual(C.hash_levels(240, 16), 2)
        self.assertEqual(C.hash_cost(240, 240, 16), 3 * 480 + 2 * 480)
        self.assertEqual(C.sort_merge_cost(240, 240, 16), 2400)
        self.assertEqual(C.sort_merge_cost(120, 80, 16), 1000)  # measured 961: the merge stopped early
        self.assertEqual(C.hash_group_cost(50, 10, 16), 50)     # the groups fit: just read the input
        self.assertGreater(C.hash_group_cost(50, 40, 16), 50)


class EnumerationTests(OptTestCase):

    def test_plans_are_left_deep(self):
        for sql in QUERIES:
            for join in join_nodes(self.optimize(sql).plan.to_json()):
                self.assertEqual(join_nodes(join['right']), [], sql)       # the right input is never a join

    def test_the_order_is_chosen_not_copied(self):
        sql = ("SELECT e.name, p.pname FROM Works w JOIN Emp e ON w.eid = e.eid JOIN Proj p ON w.pid = p.pid "
               "JOIN Dept d ON p.did = d.did WHERE d.budget = 300")
        result = self.optimize(sql, frames=4)
        table = {e['subset']: e for e in result.tables[0]}
        self.assertEqual(len(table), 15)                                    # every subset of four inputs
        self.assertEqual(table['{w,d}']['plan'], 'no join predicate, skipped')
        self.assertEqual(table['{e,p}']['plan'], 'no join predicate, skipped')
        first = join_nodes(result.plan.to_json())[-1]                       # the innermost join
        names = {first['left']['child']['child']['to'] if first['left']['op'] == 'project' else None}
        self.assertIn('d', result.tables[0][-1]['plan'].split(' outer')[0])  # Dept, filtered to one row, goes early
        self.assertNotEqual(names, {'w'})
        self.assertTrue(result.tables[0][-1]['alternatives'])
        costs = [c for _, c in result.tables[0][-1]['alternatives']]
        self.assertGreaterEqual(min(costs), result.tables[0][-1]['cost'])

    def test_six_relations(self):
        sql = ("SELECT e.name, m.name, d.title, p.pname, w.hours, x.title FROM Emp e JOIN Emp m ON e.mgr = m.eid "
               "JOIN Dept d ON e.did = d.did JOIN Works w ON w.eid = e.eid JOIN Proj p ON p.pid = w.pid "
               "JOIN Dept x ON x.did = p.did WHERE e.age = 41 AND x.budget > 800")
        logical = self.logical(sql)
        result = self.optimize(logical, frames=8)
        self.assertEqual(len(result.tables[0]), 63)
        self.assertEqual(len(join_nodes(result.plan.to_json())), 5)
        rows, _ = self.execute(result.plan.to_json(), 8)
        self.assertEqual(bag(rows), bag(self.reference(logical)))

    def test_a_cross_product_only_when_nothing_else_is_possible(self):
        sql = "SELECT a.x, b.x, c.y FROM A a, Bp b, C c WHERE a.x = b.x AND c.y < 2"
        result = self.optimize(sql)
        plan = result.plan.to_json()
        cross = [j for j in join_nodes(plan) if 'condition' not in j]
        self.assertEqual(len(cross), 1)                                     # c joins nothing: one cross product
        self.assertIn('(cross product)', result.dp_text())
        rows, _ = self.execute(plan)
        self.assertEqual(bag(rows), bag(self.reference(self.logical(sql))))
        self.assertEqual(len(rows), 5 * 2)

    def test_predicates_that_are_not_equalities(self):
        sql = "SELECT e.name, w.hours FROM Emp e JOIN Works w ON e.eid = w.eid AND w.hours > e.age WHERE e.did = 4"
        for frames, expected in [(64, ['nested_loop_join']), (4, None)]:
            plan = self.optimize(sql, frames).plan.to_json()
            join = join_nodes(plan)[0]
            if join['op'] == 'nested_loop_join':             # any condition: both parts go into the join
                self.assertIn(join['condition'], ('e.eid = w.eid and w.hours > e.age',
                                                  'w.eid = e.eid and w.hours > e.age'))
            else:                                           # an equality join, the rest filtered above it
                self.assertIn(join['condition'], ('e.eid = w.eid', 'w.eid = e.eid'))
                self.assertIn('filter', ops(plan))
            if expected:
                self.assertEqual([join['op']], expected)
        sql = "SELECT e.name, d.title FROM Emp e, Dept d WHERE e.salary < d.budget AND e.eid < 5"
        plan = self.optimize(sql).plan.to_json()
        self.assertNotIn('condition', join_nodes(plan)[0])          # rule 3 does not apply: a cross product,
        self.assertEqual(plan['child']['condition'], 'e.salary < d.budget')     # filtered above

    def test_equalities_are_written_outer_column_first(self):
        sql = "SELECT e.name FROM Dept d JOIN Emp e ON e.did = d.did"
        for frames in (4, 64):
            join = join_nodes(self.optimize(sql, frames).plan.to_json())[0]
            left = join['left']
            while 'to' not in left:
                left = left['child']
            if join['op'] != 'nested_loop_join':
                self.assertTrue(join['condition'].startswith(left['to'] + '.'), join['condition'])

    def test_assumed_estimates_change_the_plan(self):
        sql = "SELECT * FROM W1 a JOIN W2 b ON a.k = b.k WHERE a.k < 400"
        normal = self.optimize(sql, frames=6)
        forced = self.optimize(sql, frames=6, assume={'rows': {'a': 10}})
        self.assertEqual(join_nodes(normal.plan.to_json())[0]['op'], 'hash_join')
        self.assertEqual(join_nodes(forced.plan.to_json())[0]['op'], 'nested_loop_join')
        self.assertIn([n.rows for _, n in normal.plan.walk() if n.op == 'filter'], ([400], [401]))    # truth: 400
        self.assertEqual([n.rows for _, n in forced.plan.walk() if n.op == 'filter'], [10])

    def test_set_operators_get_their_columns_in_the_written_order(self):
        logical = {'op': 'union',
                   'left': {'op': 'join', 'condition': 'A.x = C.x',
                            'left': {'op': 'scan', 'table': 'A'}, 'right': {'op': 'scan', 'table': 'C'}},
                   'right': {'op': 'join', 'condition': 'Bp.x = C.x',
                             'left': {'op': 'scan', 'table': 'Bp'}, 'right': {'op': 'scan', 'table': 'C'}}}
        rows, _ = self.execute(self.optimize(logical).plan.to_json())
        self.assertEqual(bag(rows), bag(self.reference(logical)))


class PlanAndExplainTests(OptTestCase):

    def test_modes_of_group_by_and_distinct(self):
        def node(sql, op, frames):
            plan = self.optimize(sql, frames).plan.to_json()
            while plan['op'] != op:
                plan = plan['child']
            return plan
        self.assertEqual(node("SELECT did, COUNT(*) AS n FROM Emp GROUP BY did", 'group_by', 8)['mode'], 'hash')
        # 30 pages of rows that are all different. With 64 frames the table of groups fits and
        # hashing does no I/O. With 16 it does not: sorting costs 2 * 30 + 30 = 90 and the hash
        # spill 30 + 4 * 30 * (1 - 14 / 30) = 94.
        self.assertEqual(node("SELECT DISTINCT k, pad FROM W1", 'distinct', 64)['mode'], 'hash')
        self.assertEqual(node("SELECT DISTINCT k, pad FROM W1", 'distinct', 16)['mode'], 'sort')
        self.assertEqual((C.sort_cost(30, 16), C.hash_group_cost(30, 30, 16)), (90, 94))
        self.assertEqual(node("SELECT COUNT(*) FROM Emp", 'group_by', 8)['keys'], [])

    def test_explain_has_one_line_per_node_with_rows_and_cost(self):
        def costs(text):
            return [int(line.rsplit('est_cost=', 1)[1]) for line in text.splitlines()]

        result = self.optimize(JOIN_ON, frames=8)
        lines = result.explain().splitlines()
        self.assertEqual(len(lines), len(list(result.plan.walk())))
        for line in lines:
            self.assertRegex(line, r'^ *\S.* est_rows=\d+ +est_cost=\d+$')
        dept = next(line for line in lines if 'seqscan Dept' in line)
        self.assertRegex(dept, r'^ +seqscan Dept +est_rows=20 +est_cost=1$')
        scan = next(line for line in lines if 'seqscan Emp' in line)
        self.assertRegex(scan, r'est_rows=600 +est_cost=\d+$')
        # As in the specification's example: a filter and a projection cost nothing, a scan costs
        # its pages, and a join's line is the join's formula, which is not nothing.
        for line, cost in zip(lines, costs(result.explain())):
            if line.split()[0] in ('filter', 'project', 'rename'):
                self.assertEqual(cost, 0, line)
            if '_join' in line:
                self.assertGreater(cost, 0, line)
        self.assertEqual(sum(costs(result.explain(own=True))), round(result.plan.cost))     # shares add up
        self.assertEqual(costs(result.explain(subtree=True))[0], round(result.plan.cost))   # the top is the plan

    def test_the_cost_on_a_join_line_is_the_formula_of_that_join(self):
        """W1 is 30 pages and W2 is 20, and with pad selected no projection makes them smaller.
        Whatever join is chosen, its line shows its formula on those two page counts."""
        sql = "SELECT a.pad, b.pad FROM W1 a JOIN W2 b ON a.k = b.k"
        formulas = {'nested_loop_join': C.nested_loop_cost, 'hash_join': C.hash_cost,
                    'sort_merge_join': C.sort_merge_cost}
        chosen = set()
        for frames in (4, 16, 64):
            with self.subTest(frames=frames):
                result = self.optimize(sql, frames)
                join = next(n for _, n in result.plan.walk() if n.op.endswith('_join'))
                (_, outer), (_, inner) = join.children
                self.assertEqual({outer.pages, inner.pages}, {20, 30})
                expected = formulas[join.op](outer.pages, inner.pages, frames)
                line = next(l for l in result.explain().splitlines() if join.op in l)
                self.assertEqual(int(line.rsplit('est_cost=', 1)[1]), expected)
                own = next(l for l in result.explain(own=True).splitlines() if join.op in l)
                self.assertEqual(int(own.rsplit('est_cost=', 1)[1]), expected - 50)     # less the two scans
                chosen.add(join.op)
        self.assertGreater(len(chosen), 1)                      # and B changed which join it was

    def test_estimated_cost_against_what_the_executor_did(self):
        """Per operator, for plans of scans and one join: the model and the executor agree to
        within the part-filled pages."""
        sql = "SELECT e.name, w.hours FROM Emp e JOIN Works w ON e.eid = w.eid"
        for frames in (4, 6, 64):
            with self.subTest(frames=frames):
                result = self.optimize(sql, frames)
                _, execution = self.execute(result.plan.to_json(), frames)
                estimated = [(n.op, round(n.own_cost())) for _, n in result.plan.walk()]
                actual = [(op.name, op.c.page_reads + op.c.page_writes) for _, op in execution.operators()]
                self.assertEqual([o for o, _ in estimated], [o for o, _ in actual])
                for (op, e), (_, a) in zip(estimated, actual):
                    self.assertLessEqual(abs(e - a), 0.5 * a + 8, (op, e, a))

    def test_the_plan_with_no_optimizer(self):
        logical = self.logical(COMMA)
        plan = naive(logical)
        self.assertEqual(ops(plan), ['project', 'filter', 'nested_loop_join', 'rename', 'seqscan', 'rename', 'seqscan'])
        self.assertNotIn('condition', plan['child']['child'])               # the cross product, as written
        rows, slow = self.execute(plan)
        fast_rows, fast = self.execute(self.optimize(logical).plan.to_json())
        self.assertEqual(bag(rows), bag(fast_rows))
        self.assertGreater(slow.totals()['comparisons'], 10 * fast.totals()['comparisons'])


class CommandLineTests(OptTestCase):

    def opt(self, *args):
        return subprocess.run([sys.executable, 'opt.py', *args], cwd=ROOT, capture_output=True, text=True)

    def test_analyze(self):
        r = self.opt('analyze', '--db', self.dir, '--table', 'Dept', '--buckets', '5')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('Dept: n_tuples=20 n_pages=1 width=', r.stdout)
        self.assertIn('did: ndv=20 min=0 max=19 buckets=5', r.stdout)
        self.opt('analyze', '--db', self.dir, '--table', 'Dept')            # back to the default 20

    def test_plan_feeds_exec_with_no_editing(self):
        logical_path = os.path.join(self.dir, 'q.json')
        with open(logical_path, 'w') as f:
            json.dump(self.logical(JOIN_ON), f)
        r = self.opt('plan', '--db', self.dir, '--logical', logical_path, '--frames', '8', '--json')
        self.assertEqual(r.returncode, 0, r.stdout)
        physical_path = os.path.join(self.dir, 'p.json')
        with open(physical_path, 'w') as f:
            f.write(r.stdout)
        x = subprocess.run([sys.executable, 'exec.py', 'run', '--db', self.dir, '--plan', physical_path,
                            '--frames', '8', '--json'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(x.returncode, 0, x.stdout)
        rows = [tuple(row) for row in json.loads(x.stdout)['rows']]
        self.assertEqual(bag(rows), bag(self.reference(self.logical(JOIN_ON))))
        n = self.opt('plan', '--db', self.dir, '--logical', logical_path, '--frames', '8', '--naive')
        self.assertEqual(json.loads(n.stdout)['child']['op'], 'filter')

    def test_explain_rewrite_and_dp_table(self):
        r = self.opt('explain', '--db', self.dir, '--sql', COMMA, '--frames', '8')
        self.assertRegex(r.stdout.splitlines()[0], r'^project \(e\.name\) +est_rows=\d+ +est_cost=0$')
        r = self.opt('explain', '--db', self.dir, '--sql', COMMA, '--frames', '8', '--rewrite')
        self.assertIn('before\n  project [e.name]\n    select e.did = d.did and', r.stdout)
        self.assertIn('after\n  project [e.name]\n    join e.did = d.did\n', r.stdout)
        r = self.opt('explain', '--db', self.dir, '--sql', COMMA, '--frames', '8', '--rewrite', '--no-rule', '3')
        self.assertIn('    cross\n', r.stdout.split('after')[1])
        r = self.opt('dp-table', '--db', self.dir, '--sql', COMMA, '--frames', '8')
        lines = r.stdout.splitlines()
        self.assertRegex(lines[0], r'^subset +best plan considered +rows +pages +cost$')
        self.assertEqual([line.split()[0] for line in lines[1:4]], ['{e}', '{d}', '{e,d}'])
        r = self.opt('dp-table', '--db', self.dir, '--sql', 'SELECT name FROM Emp', '--frames', '8')
        self.assertEqual(r.stdout.strip(), 'no join in this plan')

    def test_dp_table_with_assumed_statistics(self):
        logical = {'op': 'join', 'condition': 'Bp.y = C.y',
                   'left': {'op': 'join', 'condition': 'A.x = Bp.x',
                            'left': {'op': 'scan', 'table': 'A'}, 'right': {'op': 'scan', 'table': 'Bp'}},
                   'right': {'op': 'scan', 'table': 'C'}}
        assume = {'rows': {'A': 5000, 'Bp': 500, 'C': 20000, 'A,Bp': 1000, 'Bp,C': 2000},
                  'pages': {'A': 100, 'Bp': 10, 'C': 400, 'A,Bp': 20, 'Bp,C': 40}}
        paths = []
        for name, content in (('l.json', logical), ('a.json', assume)):
            paths.append(os.path.join(self.dir, name))
            with open(paths[-1], 'w') as f:
                json.dump(content, f)
        r = self.opt('dp-table', '--db', self.dir, '--logical', paths[0], '--frames', '32', '--assume', paths[1])
        costs = {line.split()[0]: line.split()[-1] for line in r.stdout.splitlines()[1:]}
        self.assertEqual((costs['{A}'], costs['{Bp}'], costs['{C}'], costs['{A,Bp}'], costs['{Bp,C}'], costs['{A,Bp,C}']),
                         ('100', '10', '400', '110', '410', '530'))
        self.assertEqual(costs['{A,C}'], 'skipped')
        self.assertIn('{A,Bp} outer, nested loop with C inner: 110 + 20 + 1 x 400', r.stdout)

    def test_errors_are_one_line(self):
        for args, expected in [
                (('explain', '--db', self.dir, '--sql', 'SELECT nope FROM Emp', '--frames', '8'),
                 "error: name: unknown column 'nope'"),
                (('plan', '--db', self.dir, '--frames', '8'), 'error: give the logical plan'),
                (('plan', '--db', self.dir, '--sql', 'SELECT name FROM Emp', '--frames', '2'), 'error: planning needs'),
                (('analyze', '--db', self.dir, '--table', 'Nope'), "error: unknown table 'Nope'"),
                (('analyze', '--db', os.path.join(self.dir, 'none'), '--table', 'Emp'), 'error: there is no database')]:
            with self.subTest(expected=expected):
                r = self.opt(*args)
                self.assertEqual(r.returncode, 1)
                self.assertTrue(r.stdout.startswith(expected), r.stdout)
                self.assertNotIn('Traceback', r.stderr)
