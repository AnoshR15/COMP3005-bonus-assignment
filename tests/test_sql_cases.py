"""Module 3: the seven examples of Section 5, the fifteen required cases of Section 7, and the
checklist of Section 9. Plans are compared as whole JSON objects, then run on a little data."""
from tests.sqlbase import SqlTestCase, walk, ops
from source.condition import parse_condition
from source.plan_json import LOGICAL_OPS
from sqlfront.translate import compile_sql

SCAN_EMP = {'op': 'scan', 'table': 'Emp'}
SCAN_DEPT = {'op': 'scan', 'table': 'Dept'}


def rename(to, child):
    return {'op': 'rename', 'to': to, 'child': child}


# Section 5 examples, also used by the checklist tests.
EXAMPLES = {
    '5.1': 'SELECT name, age FROM Emp WHERE age > 30;',
    '5.2': 'SELECT e.name, d.title FROM Emp e JOIN Dept d ON e.did = d.did WHERE d.budget > 1000;',
    '5.3': 'SELECT * FROM Emp e, Dept d WHERE e.did = d.did;',
    '5.4': 'SELECT did, COUNT(*) AS n, AVG(salary) AS avg_sal FROM Emp WHERE age > 25 GROUP BY did '
           'HAVING COUNT(*) > 5 ORDER BY n DESC LIMIT 10;',
    '5.5': 'SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid;',
    '5.6': 'SELECT t.did, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t WHERE t.n > 3;',
    '5.7a': 'SELECT city FROM Emp UNION ALL SELECT city FROM Dept;',
    '5.7b': 'SELECT city FROM Emp UNION SELECT city FROM Dept;',
}
MORE = [
    'SELECT DISTINCT city FROM Emp ORDER BY city',
    'SELECT name FROM Emp ORDER BY age DESC LIMIT 2',
    'SELECT name AS who, age FROM Emp WHERE NOT (age <= 30 OR name = \'O\'\'Brien\') ORDER BY who',
    'SELECT e.name, d.title, p.pname FROM Emp e, Dept d, Proj p WHERE e.did = d.did AND p.did = d.did',
    'SELECT city, MIN(age), MAX(name), SUM(salary) FROM Emp GROUP BY city HAVING SUM(salary) > 100',
    'SELECT name FROM Emp WHERE age > 30 INTERSECT SELECT name FROM Emp WHERE did = 10 '
    'EXCEPT SELECT title FROM Dept',
    'SELECT COUNT(*) FROM Emp',
]


class ExampleTests(SqlTestCase):
    """Every example in Section 5 reproduces as a plan with the same shape."""

    def test_5_1_filter_and_projection(self):
        self.assertEqual(self.compile(EXAMPLES['5.1']), {
            'op': 'project', 'columns': ['Emp.name', 'Emp.age'],
            'child': {'op': 'select', 'condition': 'Emp.age > 30', 'child': SCAN_EMP}})
        # No distinct was written, so the two Bobs aged 41 both appear.
        self.assertEqual(sorted(self.run_sql(EXAMPLES['5.1'])),
                         [('Ann', 31), ('Bob', 41), ('Bob', 41), ('Di', 55)])

    def test_5_2_join_with_aliases(self):
        self.assertEqual(self.compile(EXAMPLES['5.2']), {
            'op': 'project', 'columns': ['e.name', 'd.title'],
            'child': {'op': 'select', 'condition': 'd.budget > 1000',
                      'child': {'op': 'join', 'condition': 'e.did = d.did',
                                'left': rename('e', SCAN_EMP), 'right': rename('d', SCAN_DEPT)}}})
        self.assertEqual(sorted(self.run_sql(EXAMPLES['5.2'])), [('Ann', 'Eng'), ('Bob', 'Eng'), ('Di', 'Law')])

    def test_5_3_comma_join(self):
        plan = self.compile(EXAMPLES['5.3'])
        self.assertEqual(plan['child'], {
            'op': 'select', 'condition': 'e.did = d.did',
            'child': {'op': 'cross', 'left': rename('e', SCAN_EMP), 'right': rename('d', SCAN_DEPT)}})
        self.assertEqual(plan['columns'], ['e.eid', 'e.name', 'e.age', 'e.salary', 'e.did', 'e.mgr', 'e.city',
                                           'd.did', 'd.title', 'd.budget', 'd.city'])
        self.assertEqual(len(self.run_sql(EXAMPLES['5.3'])), 5)

    def test_5_4_grouping_with_having(self):
        self.assertEqual(self.compile(EXAMPLES['5.4']), {
            'op': 'limit', 'count': 10,
            'child': {'op': 'sort', 'keys': [['n', 'desc']],
                      'child': {'op': 'project', 'columns': ['Emp.did', 'n', 'avg_sal'],
                                'child': {'op': 'select', 'condition': 'n > 5',
                                          'child': {'op': 'group_by', 'keys': ['Emp.did'],
                                                    'aggregates': [['count', '*', 'n'],
                                                                   ['avg', 'Emp.salary', 'avg_sal']],
                                                    'child': {'op': 'select', 'condition': 'Emp.age > 25',
                                                              'child': SCAN_EMP}}}}}})
        # The same query with a threshold the five test rows can pass.
        self.assertEqual(self.run_sql(EXAMPLES['5.4'].replace('> 5', '> 0')),
                         [(10, 2, 400), (20, 1, 700), (30, 1, 900)])

    def test_5_5_self_join(self):
        self.assertEqual(self.compile(EXAMPLES['5.5'])['child'], {
            'op': 'join', 'condition': 'e.mgr = m.eid',
            'left': rename('e', SCAN_EMP), 'right': rename('m', SCAN_EMP)})

    def test_5_6_subquery_in_from(self):
        self.assertEqual(self.compile(EXAMPLES['5.6']), {
            'op': 'project', 'columns': ['t.did', 't.n'],
            'child': {'op': 'select', 'condition': 't.n > 3',
                      'child': rename('t', {
                          'op': 'project', 'columns': ['Emp.did', 'n'],
                          'child': {'op': 'group_by', 'keys': ['Emp.did'],
                                    'aggregates': [['count', '*', 'n']], 'child': SCAN_EMP}})}})
        self.assertEqual(sorted(self.run_sql(EXAMPLES['5.6'].replace('> 3', '> 1'))), [(10, 2), (20, 2)])

    def test_5_7_the_two_unions(self):
        left = {'op': 'project', 'columns': ['Emp.city'], 'child': SCAN_EMP}
        right = {'op': 'project', 'columns': ['Dept.city'], 'child': SCAN_DEPT}
        self.assertEqual(self.compile(EXAMPLES['5.7a']), {'op': 'union_all', 'left': left, 'right': right})
        self.assertEqual(self.compile(EXAMPLES['5.7b']), {'op': 'union', 'left': left, 'right': right})


class RequiredCaseTests(SqlTestCase):

    def test_case_01_self_join_is_two_renames_over_one_table(self):
        plan = self.compile(EXAMPLES['5.5'])
        self.assertEqual(ops(plan), ['project', 'join', 'rename', 'scan', 'rename', 'scan'])
        self.assertEqual([n['to'] for n in walk(plan) if n['op'] == 'rename'], ['e', 'm'])
        self.assertEqual({n['table'] for n in walk(plan) if n['op'] == 'scan'}, {'Emp'})
        self.assertEqual(sorted(self.run_sql(EXAMPLES['5.5'])),
                         [('Ann', 'Ann'), ('Bob', 'Ann'), ('Bob', 'Bob'), ('Cy', 'Bob'), ('Di', 'Ann')])

    def test_case_02_select_star_over_three_tables_qualifies_shared_names(self):
        plan = self.compile('SELECT * FROM Emp, Dept, Proj WHERE Emp.did = Dept.did AND Proj.did = Dept.did')
        columns = plan['columns']
        self.assertEqual(len(columns), 7 + 4 + 3)                       # FROM order, then schema order
        self.assertEqual(columns[:2] + columns[7:9] + columns[11:], [
            'Emp.eid', 'Emp.name', 'Dept.did', 'Dept.title', 'Proj.pid', 'Proj.did', 'Proj.pname'])
        for shared in ('Emp.did', 'Dept.did', 'Proj.did', 'Emp.city', 'Dept.city'):
            self.assertIn(shared, columns)
        self.assertEqual(len(set(columns)), len(columns))

    def test_case_03_unqualified_column_in_two_tables_is_ambiguous(self):
        e = self.error('SELECT name FROM Emp e, Dept d\n WHERE did = 10', 'ambiguity')
        self.assertIn('e.did or d.did', e.message)
        self.assertEqual((e.line, e.column), (2, 8))
        self.error('SELECT city FROM Emp, Dept', 'ambiguity')
        self.compile('SELECT Emp.city, title FROM Emp, Dept')            # title is in one table only

    def test_case_04_union_and_union_all_give_different_plans(self):
        a = self.compile(EXAMPLES['5.7a'])
        b = self.compile(EXAMPLES['5.7b'])
        self.assertNotEqual(a, b)
        self.assertEqual((a['op'], b['op']), ('union_all', 'union'))
        self.assertEqual((a['left'], a['right']), (b['left'], b['right']))
        self.assertEqual(len(self.run_sql(EXAMPLES['5.7a'])), 5 + 3)
        self.assertEqual(sorted(self.run_sql(EXAMPLES['5.7b'])), [('Hull',), ('Ottawa',), ('Toronto',)])

    def test_case_05_where_below_the_grouping_and_having_above(self):
        plan = self.compile('SELECT did, COUNT(*) FROM Emp WHERE age > 25 GROUP BY did HAVING COUNT(*) > 1')
        self.assertEqual(ops(plan), ['project', 'select', 'group_by', 'select', 'scan'])
        having, where = [n for n in walk(plan) if n['op'] == 'select']
        self.assertEqual((having['condition'], where['condition']), ('count_star > 1', 'Emp.age > 25'))
        self.assertEqual(plan['columns'], ['Emp.did', 'count_star'])
        self.assertEqual(self.run_sql('SELECT did, COUNT(*) FROM Emp WHERE age > 25 GROUP BY did '
                                      'HAVING COUNT(*) > 1'), [(10, 2)])

    def test_case_06_non_grouped_column_in_the_select_list(self):
        e = self.error('SELECT did, name, COUNT(*) FROM Emp GROUP BY did', 'aggregate')
        self.assertIn("'Emp.name'", e.message)
        self.assertEqual(e.column, 13)
        self.error('SELECT name, COUNT(*) FROM Emp', 'aggregate')           # aggregating without GROUP BY
        self.error('SELECT did FROM Emp GROUP BY did HAVING age > 1', 'aggregate')
        self.error('SELECT * FROM Emp GROUP BY did', 'aggregate')

    def test_case_07_aggregate_in_where(self):
        e = self.error('SELECT did FROM Emp WHERE COUNT(*) > 1 GROUP BY did', 'aggregate')
        self.assertIn('WHERE', e.message)
        self.error('SELECT name FROM Emp WHERE MAX(age) = age', 'aggregate')
        self.error('SELECT e.name FROM Emp e JOIN Dept d ON SUM(e.did) = d.did', 'aggregate')

    def test_case_08_subquery_in_from_joined_to_a_base_table(self):
        sql = ('SELECT d.title, t.n FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t '
               'JOIN Dept d ON t.did = d.did WHERE t.n > 1')
        plan = self.compile(sql)
        join = plan['child']['child']
        self.assertEqual((join['op'], join['condition']), ('join', 't.did = d.did'))
        self.assertEqual(ops(join['left']), ['rename', 'project', 'group_by', 'scan'])
        self.assertEqual(join['right'], rename('d', SCAN_DEPT))
        self.assertEqual(sorted(self.run_sql(sql)), [('Eng', 2), ('Ops', 2)])
        e = self.error('SELECT t.age FROM (SELECT did FROM Emp) AS t', 'name')   # only the output is visible
        self.assertIn("unknown column 'age' in 't'", e.message)

    def test_case_09_order_by_a_column_that_is_not_in_the_select_list(self):
        sql = 'SELECT name FROM Emp ORDER BY salary DESC'
        self.assertEqual(self.compile(sql), {
            'op': 'project', 'columns': ['Emp.name'],
            'child': {'op': 'sort', 'keys': [['Emp.salary', 'desc']],
                      'child': {'op': 'project', 'columns': ['Emp.name', 'Emp.salary'], 'child': SCAN_EMP}}})
        self.assertEqual(self.run_sql(sql), [('Di',), ('Bob',), ('Ann',), ('Bob',), ('Cy',)])
        # A column that is in the select list needs no second project; an alias is found first.
        self.assertEqual(ops(self.compile('SELECT name, age FROM Emp ORDER BY age')), ['sort', 'project', 'scan'])
        self.assertEqual(self.compile('SELECT name AS who FROM Emp ORDER BY who')['keys'], [['who', 'asc']])
        self.error('SELECT DISTINCT name FROM Emp ORDER BY salary', 'schema')

    def test_case_10_int_compared_with_a_string(self):
        e = self.error("SELECT name FROM Emp WHERE age > '30'", 'type')
        self.assertEqual(str(e), "error: type: cannot compare INT with VARCHAR in 'Emp.age > '30'' "
                                 'at line 1, column 32')
        self.error("SELECT name FROM Emp WHERE name = 30", 'type')
        self.error('SELECT name FROM Emp WHERE name = age', 'type')           # equality too
        self.error('SELECT SUM(name) FROM Emp', 'type')
        self.error('SELECT did FROM Emp GROUP BY did HAVING MIN(name) > 3', 'type')
        self.compile('SELECT MIN(name), MAX(age), COUNT(name) FROM Emp')

    def test_case_11_unknown_table_and_unknown_column_have_different_messages(self):
        table = self.error('SELECT name FROM Staff', 'name')
        column = self.error('SELECT wage FROM Emp', 'name')
        self.assertEqual(table.message, "unknown table 'Staff'")
        self.assertEqual(column.message, "unknown column 'wage'")
        self.assertIn("unknown column 'wage' in 'e'", self.error('SELECT e.wage FROM Emp e', 'name').message)
        self.assertIn("unknown table or alias 'x'", self.error('SELECT x.name FROM Emp e', 'name').message)
        # An alias hides the table name: Emp is not in scope once it is called e.
        self.assertIn("unknown table or alias 'Emp'", self.error('SELECT Emp.name FROM Emp e', 'name').message)

    def test_case_12_the_same_table_twice_without_aliases(self):
        e = self.error('SELECT * FROM Emp, Emp', 'name')
        self.assertIn("duplicate alias 'Emp'", e.message)
        self.error('SELECT * FROM Emp e, Dept e', 'name')
        self.compile('SELECT * FROM Emp e, Emp m')

    def test_case_13_count_star_without_group_by_gives_one_row(self):
        sql = 'SELECT COUNT(*) FROM Emp'
        self.assertEqual(self.compile(sql), {
            'op': 'project', 'columns': ['count_star'],
            'child': {'op': 'group_by', 'keys': [], 'aggregates': [['count', '*', 'count_star']],
                      'child': SCAN_EMP}})
        self.assertEqual(self.run_sql(sql), [(5,)])
        self.assertEqual(self.run_sql(sql, rows={'Emp': []}), [(0,)])        # one row even when Emp is empty
        self.assertEqual(self.run_sql('SELECT did, COUNT(*) FROM Emp GROUP BY did', rows={'Emp': []}), [])

    def test_case_14_union_of_different_column_types(self):
        e = self.error('SELECT name FROM Emp UNION SELECT did FROM Dept', 'schema')
        self.assertIn('column 1 is VARCHAR', e.message)
        self.assertIn('2 columns', self.error('SELECT name, age FROM Emp EXCEPT SELECT title FROM Dept',
                                              'schema').message)
        self.compile('SELECT name FROM Emp UNION SELECT title FROM Dept')    # names may differ, types may not

    def test_case_15_four_table_chain_with_commas_and_with_join_on(self):
        commas = self.compile('SELECT e.name, p.pname FROM Emp e, Dept d, Proj p, Works w '
                              'WHERE e.did = d.did AND p.did = d.did AND w.pid = p.pid')
        joins = self.compile('SELECT e.name, p.pname FROM Emp e JOIN Dept d ON e.did = d.did '
                             'JOIN Proj p ON p.did = d.did JOIN Works w ON w.pid = p.pid')
        self.assertEqual(ops(commas), ['project', 'select', 'cross', 'cross', 'cross',
                                       'rename', 'scan', 'rename', 'scan', 'rename', 'scan', 'rename', 'scan'])
        self.assertEqual(ops(joins), ['project', 'join', 'join', 'join',
                                      'rename', 'scan', 'rename', 'scan', 'rename', 'scan', 'rename', 'scan'])
        self.assertEqual(commas['child']['condition'], 'e.did = d.did and p.did = d.did and w.pid = p.pid')
        self.assertEqual([n['condition'] for n in walk(joins) if n['op'] == 'join'],
                         ['w.pid = p.pid', 'p.did = d.did', 'e.did = d.did'])     # outermost join first

        def leaves(plan):
            return [(n['to'], n['child']['table']) for n in walk(plan) if n['op'] == 'rename']
        self.assertEqual(leaves(commas), leaves(joins))                      # same tables, same order
        self.assertEqual(commas['columns'], joins['columns'])
        both = [sorted(self.run_sql(s)) for s in (
            'SELECT e.name, p.pname FROM Emp e, Dept d, Proj p, Works w '
            'WHERE e.did = d.did AND p.did = d.did AND w.pid = p.pid',
            'SELECT e.name, p.pname FROM Emp e JOIN Dept d ON e.did = d.did '
            'JOIN Proj p ON p.did = d.did JOIN Works w ON w.pid = p.pid')]
        self.assertEqual(both[0], both[1])
        self.assertEqual(len(both[0]), 6)


class BagTests(SqlTestCase):
    """Section 4: where duplicates are removed, and where they are not."""

    def test_project_never_brings_a_distinct_with_it(self):
        self.assertNotIn('distinct', ops(self.compile('SELECT city FROM Emp')))
        self.assertEqual(len(self.run_sql('SELECT city FROM Emp')), 5)

    def test_select_distinct_puts_distinct_above_the_projection(self):
        plan = self.compile('SELECT DISTINCT city FROM Emp ORDER BY city LIMIT 5')
        self.assertEqual(ops(plan), ['limit', 'sort', 'distinct', 'project', 'scan'])
        self.assertEqual(self.run_sql('SELECT DISTINCT city FROM Emp ORDER BY city'), [('Ottawa',), ('Toronto',)])

    def test_distinct_above_a_group_by_is_left_out_when_it_is_redundant(self):
        self.assertNotIn('distinct', ops(self.compile('SELECT DISTINCT did, COUNT(*) FROM Emp GROUP BY did')))
        self.assertNotIn('distinct', ops(self.compile('SELECT DISTINCT COUNT(*) FROM Emp')))
        # Not redundant: the grouping column is projected away, so two groups can give equal rows.
        sql = 'SELECT DISTINCT COUNT(*) FROM Emp GROUP BY did'
        self.assertIn('distinct', ops(self.compile(sql)))
        self.assertEqual(sorted(self.run_sql(sql)), [(1,), (2,)])
        self.assertEqual(sorted(self.run_sql('SELECT COUNT(*) FROM Emp GROUP BY did')), [(1,), (2,), (2,)])

    def test_intersect_and_except_have_set_semantics(self):
        self.assertEqual(self.run_sql('SELECT name FROM Emp INTERSECT SELECT name FROM Emp'),
                         [('Ann',), ('Bob',), ('Cy',), ('Di',)])
        self.assertEqual(self.run_sql("SELECT name FROM Emp EXCEPT SELECT name FROM Emp WHERE name <> 'Bob'"),
                         [('Bob',)])

    def test_set_operators_chain_to_the_left(self):
        plan = self.compile('SELECT name FROM Emp UNION SELECT title FROM Dept EXCEPT SELECT city FROM Dept')
        self.assertEqual((plan['op'], plan['left']['op']), ('except', 'union'))


class ChecklistTests(SqlTestCase):
    """Section 9."""

    def plans(self):
        for sql in list(EXAMPLES.values()) + MORE:
            yield sql, self.compile(sql)

    def test_only_logical_operators_with_the_documented_fields(self):
        logical = {'scan', 'select', 'project', 'join', 'cross', 'rename', 'group_by', 'distinct', 'sort',
                   'limit', 'union', 'union_all', 'intersect', 'except'}
        self.assertLessEqual(logical, set(LOGICAL_OPS))
        seen = set()
        for sql, plan in self.plans():
            for node in walk(plan):
                self.assertIn(node['op'], logical, sql)
                seen.add(node['op'])
        self.assertEqual(seen, logical)                     # and the test queries exercise all fourteen

    def test_no_unqualified_column_except_names_the_query_created(self):
        for sql, plan in self.plans():
            with self.subTest(sql=sql):
                created = set()
                for node in walk(plan):
                    if node['op'] == 'group_by':
                        created |= {name for _, _, name in node['aggregates']}
                    if node['op'] == 'project':
                        created |= {c[1] for c in node['columns'] if isinstance(c, list)}
                for node in walk(plan):
                    names = []
                    if node['op'] == 'project':
                        names += [c[0] if isinstance(c, list) else c for c in node['columns']]
                    if node['op'] == 'group_by':
                        names += node['keys'] + [arg for _, arg, _ in node['aggregates'] if arg != '*']
                    if node['op'] == 'sort':
                        names += [k for k, _ in node['keys']]
                    if 'condition' in node:
                        names += attributes(parse_condition(node['condition']))
                    for name in names:
                        self.assertTrue('.' in name or name in created, f'{name} in {node["op"]}')

    def test_every_condition_is_read_back_by_the_condition_parser(self):
        for sql, plan in self.plans():
            for node in walk(plan):
                if 'condition' in node:
                    parse_condition(node['condition'])

    def test_every_plan_loads_and_runs(self):
        for sql, plan in self.plans():
            with self.subTest(sql=sql):
                self.run_sql(sql)

    def test_predicates_stay_where_the_sql_put_them(self):
        # d.budget touches only Dept, and still sits above the join.
        plan = self.compile(EXAMPLES['5.2'])
        self.assertEqual(ops(plan)[:3], ['project', 'select', 'join'])
        # One select carries the whole WHERE; the conjunction is not split.
        plan = self.compile('SELECT name FROM Emp WHERE age > 1 AND did = 2 AND salary < 3')
        self.assertEqual(ops(plan), ['project', 'select', 'scan'])
        self.assertEqual(plan['child']['condition'], 'Emp.age > 1 and Emp.did = 2 and Emp.salary < 3')
        # Grouping written by the query's parentheses survives.
        plan = self.compile('SELECT name FROM Emp WHERE age > 1 AND (did = 2 OR NOT salary <> 3)')
        self.assertEqual(plan['child']['condition'], 'Emp.age > 1 and (Emp.did = 2 or not Emp.salary <> 3)')


def attributes(cond):
    """The attribute names used in a condition tree of the engine."""
    from source.ast import Attr, Literal, Compare
    if isinstance(cond, Compare):
        return [f'{x.relation}.{x.name}' if x.relation else x.name
                for x in (cond.left, cond.right) if isinstance(x, Attr)]
    if cond.op == 'not':
        return attributes(cond.child)
    return attributes(cond.left) + attributes(cond.right)


class StatementTests(SqlTestCase):

    def test_create_then_insert_then_query_in_one_file(self):
        plans = compile_sql("CREATE TABLE Tmp (a INT, b VARCHAR(5));\n"
                            "INSERT INTO Tmp VALUES (1, 'x');\n"
                            "SELECT b FROM Tmp WHERE a = 1;", self.catalog())
        self.assertEqual(plans[0], {'op': 'create_table', 'table': 'Tmp',
                                    'columns': [['a', 'INT'], ['b', 'VARCHAR(5)']]})
        self.assertEqual(plans[1], {'op': 'insert', 'table': 'Tmp', 'values': [1, 'x']})
        self.assertEqual(plans[2]['columns'], ['Tmp.b'])

    def test_statement_errors(self):
        self.error('CREATE TABLE Emp (a INT)', 'schema')                       # already exists
        self.error('CREATE TABLE T (a INT, a INT)', 'schema')
        self.error('CREATE TABLE T (a VARCHAR(300))', 'schema')
        self.error('INSERT INTO Nope VALUES (1)', 'name')
        self.error('INSERT INTO Proj VALUES (1, 2)', 'schema')                 # three columns
        self.error("INSERT INTO Proj VALUES (1, 'x', 'y')", 'type')
        self.error("INSERT INTO Proj VALUES (1, 2, 'a name far longer than thirty characters')", 'type')

    def test_aggregate_names(self):
        plan = self.compile('SELECT COUNT(*), COUNT(*) AS n, AVG(salary), MIN(Emp.name) FROM Emp '
                            'GROUP BY did HAVING MAX(age) > 1 AND COUNT(*) > 0')
        group = [n for n in walk(plan) if n['op'] == 'group_by'][0]
        self.assertEqual(group['aggregates'], [
            ['count', '*', 'count_star'], ['count', '*', 'n'], ['avg', 'Emp.salary', 'avg_Emp_salary'],
            ['min', 'Emp.name', 'min_Emp_name'], ['max', 'Emp.age', 'max_Emp_age']])
        self.assertEqual(plan['columns'], ['count_star', 'n', 'avg_Emp_salary', 'min_Emp_name'])
        self.assertEqual(plan['child']['condition'], 'max_Emp_age > 1 and count_star > 0')
        self.error('SELECT SUM(*) FROM Emp', 'aggregate')
        self.error('SELECT COUNT(*) AS n, SUM(age) AS n FROM Emp', 'schema')

    def test_select_alias_on_a_plain_column(self):
        plan = self.compile('SELECT name AS who, age FROM Emp')
        self.assertEqual(plan['columns'], [['Emp.name', 'who'], 'Emp.age'])
        sql = 'SELECT t.who FROM (SELECT name AS who FROM Emp WHERE age > 50) AS t'
        self.assertEqual(self.run_sql(sql), [('Di',)])
        self.error('SELECT t.name FROM (SELECT e.name, m.name FROM Emp e, Emp m) AS t', 'schema')
