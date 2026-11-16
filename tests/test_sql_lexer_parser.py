"""The SQL scanner and parser: Sections 1 and 2 of the Module 3 specification."""
import unittest
from sqlfront.errors import SqlError
from sqlfront.explain import syntax_tree, condition_sql
from sqlfront.lexer import scan
from sqlfront.nodes import SetOp, Select, FromJoin, TableRef, SubqueryRef, CreateTable, Insert
from sqlfront.parser import parse_sql


def tokens(text):
    return [(t.kind, t.value) for t in scan(text) if t.kind != 'EOF']


def one(sql):
    statements = parse_sql(sql)
    assert len(statements) == 1
    return statements[0]


def where(condition):
    return one(f'SELECT a FROM T WHERE {condition}').where


class LexerTests(unittest.TestCase):

    def test_keywords_are_case_insensitive(self):
        self.assertEqual(tokens('SELECT select SeLeCt'), [('KEYWORD', 'SELECT')] * 3)

    def test_identifiers_are_case_sensitive(self):
        self.assertEqual(tokens('Emp emp _x1'), [('IDENT', 'Emp'), ('IDENT', 'emp'), ('IDENT', '_x1')])

    def test_strings(self):
        self.assertEqual(tokens("'O''Brien' 'a,b)' '' '--x'"),
                         [('STRING', "O'Brien"), ('STRING', 'a,b)'), ('STRING', ''), ('STRING', '--x')])

    def test_numbers_are_integers_with_an_optional_sign(self):
        self.assertEqual(tokens('30 -30 +7 age>-5'),
                         [('INT', 30), ('INT', -30), ('INT', 7), ('IDENT', 'age'), ('OP', '>'), ('INT', -5)])

    def test_maximal_munch(self):
        self.assertEqual([v for k, v in tokens('<= >= <> < > = <=>')], ['<=', '>=', '<>', '<', '>', '=', '<=', '>'])
        self.assertEqual(tokens('a<>b'), [('IDENT', 'a'), ('OP', '<>'), ('IDENT', 'b')])

    def test_comments_run_to_the_end_of_the_line(self):
        self.assertEqual(tokens('a -- b c\n-- whole line\nd'), [('IDENT', 'a'), ('IDENT', 'd')])

    def test_positions_are_line_and_column(self):
        toks = scan('SELECT a\n  FROM T -- c\n WHERE a = 1')
        self.assertEqual([t.pos for t in toks],
                         [(1, 1), (1, 8), (2, 3), (2, 8), (3, 2), (3, 8), (3, 10), (3, 12), (3, 13)])

    def test_lexical_errors(self):
        for text, column in [("SELECT 'abc", 8), ('a ! b', 3), ('a # b', 3), ('12abc', 1), ('1.5', 1),
                             ('99999999999', 1), ('a - b', 3)]:
            with self.subTest(text=text):
                with self.assertRaises(SqlError) as cm:
                    scan(text)
                self.assertEqual(cm.exception.category, 'lexical')
                self.assertEqual((cm.exception.line, cm.exception.column), (1, column))

    def test_a_bad_number_is_quoted_whole(self):
        for text, quoted in [('age > 1.5 AND', "'1.5'"), ('12abc)', "'12abc'"), ('-3.25', "'-3.25'")]:
            with self.subTest(text=text):
                with self.assertRaises(SqlError) as cm:
                    scan(text)
                self.assertIn(f'invalid number {quoted}; numbers are integers', str(cm.exception))


class ParserTests(unittest.TestCase):

    def test_set_operators_are_left_associative_on_one_level(self):
        q = one('SELECT a FROM A UNION SELECT a FROM B EXCEPT SELECT a FROM C')
        self.assertEqual((q.op, q.left.op), ('except', 'union'))            # (A UNION B) EXCEPT C
        q = one('SELECT a FROM A EXCEPT SELECT a FROM B INTERSECT SELECT a FROM C')
        self.assertEqual((q.op, q.left.op), ('intersect', 'except'))        # no extra precedence for INTERSECT
        q = one('SELECT a FROM A UNION (SELECT a FROM B EXCEPT SELECT a FROM C)')
        self.assertEqual((q.op, q.right.op), ('union', 'except'))           # parentheses override

    def test_union_all_is_its_own_operator(self):
        self.assertEqual(one('SELECT a FROM A UNION ALL SELECT a FROM B').op, 'union_all')
        self.assertEqual(one('SELECT a FROM A UNION SELECT a FROM B').op, 'union')

    def test_condition_precedence_not_then_and_then_or(self):
        self.assertEqual(condition_sql(where('a = 1 OR b = 2 AND NOT c = 3')), 'a = 1 OR b = 2 AND NOT c = 3')
        c = where('a = 1 OR b = 2 AND NOT c = 3')
        self.assertEqual((c.op, c.right.op), ('or', 'and'))                 # a=1 OR (b=2 AND (NOT c=3))
        c = where('NOT a = 1 AND b = 2')
        self.assertEqual(c.op, 'and')                                       # (NOT a=1) AND b=2
        c = where('(a = 1 OR b = 2) AND c = 3')
        self.assertEqual((c.op, c.left.op), ('and', 'or'))
        self.assertEqual(condition_sql(c), '(a = 1 OR b = 2) AND c = 3')

    def test_and_and_or_are_left_associative(self):
        c = where('a = 1 AND b = 2 AND c = 3')
        self.assertEqual(c.left.op, 'and')
        c = where('a = 1 OR b = 2 OR c = 3')
        self.assertEqual(c.left.op, 'or')

    def test_from_comma_and_join_chain_to_the_left(self):
        s = one('SELECT * FROM A a, B JOIN C AS c ON a.x = c.x')
        self.assertIsInstance(s.source, FromJoin)
        self.assertEqual((s.source.kind, s.source.left.kind), ('join', 'cross'))
        self.assertEqual(s.source.left.left, TableRef('A', 'a', (1, 15)))
        self.assertEqual(s.source.right.alias, 'c')

    def test_subquery_in_from_needs_an_alias(self):
        s = one('SELECT t.a FROM (SELECT a FROM T) AS t')
        self.assertIsInstance(s.source, SubqueryRef)
        self.assertIsInstance(s.source.query, Select)
        with self.assertRaises(SqlError) as cm:
            parse_sql('SELECT a FROM (SELECT a FROM T)')
        self.assertEqual(cm.exception.category, 'syntax')
        self.assertIn('must have an alias', cm.exception.message)

    def test_all_clauses(self):
        s = one('select distinct a, count(*) as n from T where a > 1 group by a having count(*) > 2 '
                'order by n desc, a limit 5')
        self.assertTrue(s.distinct)
        self.assertEqual([i.alias for i in s.items], [None, 'n'])
        self.assertEqual([(o.column.name, o.descending) for o in s.order_by], [('n', True), ('a', False)])
        self.assertEqual(s.limit, 5)
        self.assertEqual(syntax_tree(s).splitlines(), [
            'select distinct',
            '  columns: a, COUNT(*) AS n',
            '  from:',
            '    table T',
            '  where: a > 1',
            '  group by: a',
            '  having: COUNT(*) > 2',
            '  order by: n DESC, a ASC',
            '  limit: 5'])

    def test_create_insert_and_several_statements(self):
        statements = parse_sql("CREATE TABLE T (a INT, b VARCHAR(20));\n"
                               "INSERT INTO T VALUES (1, 'x');;\n"
                               "SELECT * FROM T;")
        self.assertEqual([type(s) for s in statements], [CreateTable, Insert, Select])
        self.assertEqual([c[:3] for c in statements[0].columns], [('a', 'INT', None), ('b', 'VARCHAR', 20)])
        self.assertEqual([v.value for v in statements[1].values], [1, 'x'])
        self.assertIsNone(statements[2].items)
        self.assertEqual(parse_sql(''), [])

    def test_explain_shows_union_and_union_all_differently(self):
        a = syntax_tree(one('SELECT city FROM Emp UNION SELECT city FROM Dept'))
        b = syntax_tree(one('SELECT city FROM Emp UNION ALL SELECT city FROM Dept'))
        self.assertEqual(a.splitlines()[0], 'union')
        self.assertEqual(b.splitlines()[0], 'union_all')
        self.assertEqual(a.splitlines()[1:], b.splitlines()[1:])

    def test_syntax_errors_say_what_was_expected_and_where(self):
        for sql, pos, expected in [
                ('SELECT a FROM', (1, 14), 'a table name'),
                ('SELECT FROM T', (1, 8), 'a column or an aggregate'),
                ('SELECT a T', (1, 10), 'FROM'),
                ('SELECT a FROM T WHERE a >', (1, 26), 'a column, a number or a string'),
                ('SELECT a FROM T WHERE a 1', (1, 25), 'a comparison operator'),
                ('SELECT a FROM T WHERE (a = 1', (1, 29), "')'"),
                ('SELECT a FROM T JOIN U', (1, 23), 'ON'),
                ('SELECT a FROM T GROUP a', (1, 23), 'BY'),
                ('SELECT a FROM T HAVING a = 1', (1, 17), 'end of input'),      # HAVING needs GROUP BY
                ('SELECT a FROM T LIMIT x', (1, 23), 'a row count'),
                ('SELECT a FROM T\nUNION', (2, 6), 'SELECT'),
                ('SELECT a FROM select', (1, 15), 'reserved word'),
                ('SELECT COUNT(1) FROM T', (1, 14), 'a column name'),
                ('CREATE TABLE T (a FLOAT)', (1, 19), 'a type'),
                ('INSERT INTO T VALUES (a)', (1, 23), 'a number or a string'),
                ('SELECT a FROM T SELECT b FROM T', (1, 17), "';'"),
                ('DROP TABLE T', (1, 1), 'SELECT, CREATE or INSERT')]:
            with self.subTest(sql=sql):
                with self.assertRaises(SqlError) as cm:
                    parse_sql(sql)
                e = cm.exception
                self.assertEqual(e.category, 'syntax')
                self.assertEqual((e.line, e.column), pos)
                self.assertIn(expected, e.message)

    def test_error_format(self):
        with self.assertRaises(SqlError) as cm:
            parse_sql('SELECT a\n  FROM T WHERE a >')
        self.assertEqual(str(cm.exception),
                         'error: syntax: expected a column, a number or a string, found end of input '
                         'at line 2, column 19')
