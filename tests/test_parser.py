"""Parser: required cases 10 to 13 and 15 to 17, the syntax error category, and the keyword rule
of GRAMMAR.md section 6."""
from tests.base import RATestCase
from source.errors import ParseError


class PrecedenceTests(RATestCase):
    files = ('tests/data/sets.ra',)      # A = {1, 2, 3}, B = {1}, C = {2}, D = {2, 3}

    def test_union_then_minus_groups_to_the_left(self):
        """Case 10."""
        self.assertEqual(self.tree('A union B minus C'),
                         'Minus\n├── Union\n│   ├── Relation(A)\n│   └── Relation(B)\n└── Relation(C)')
        self.assertEqual(self.rows('A union B minus C'), {(1,), (3,)})            # (A union B) minus C
        self.assertEqual(self.rows('A union (B minus C)'), {(1,), (2,), (3,)})    # the other grouping

    def test_minus_is_left_associative(self):
        """Case 11."""
        self.assertEqual(self.tree('A minus B minus C'),
                         'Minus\n├── Minus\n│   ├── Relation(A)\n│   └── Relation(B)\n└── Relation(C)')
        self.assertEqual(self.rows('A minus B minus C'), {(3,)})                  # (A minus B) minus C
        self.assertEqual(self.rows('A minus (B minus C)'), {(2,), (3,)})          # the other grouping

    def test_not_binds_tighter_than_and_tighter_than_or(self):
        """Case 12."""
        self.assertEqual(
            self.tree('select[not (a=1 and b=2) or c>3](R)').splitlines()[0],
            'Select(cond=Or(Not(And(Eq(Attr(a), Num(1)), Eq(Attr(b), Num(2)))), Gt(Attr(c), Num(3))))')

    def test_and_groups_before_or(self):
        """Case 13."""
        self.assertEqual(
            self.tree('select[a=1 and b=2 or c=3](R)').splitlines()[0],
            'Select(cond=Or(And(Eq(Attr(a), Num(1)), Eq(Attr(b), Num(2))), Eq(Attr(c), Num(3))))')

    def test_parentheses_override_precedence(self):
        """Case 15."""
        self.assertEqual(self.tree('(A union B) minus (C intersect D)'),
                         'Minus\n├── Union\n│   ├── Relation(A)\n│   └── Relation(B)\n'
                         '└── Intersect\n    ├── Relation(C)\n    └── Relation(D)')
        self.assertEqual(self.rows('(A union B) minus (C intersect D)'), {(1,), (3,)})


class SyntaxErrorTests(RATestCase):

    def test_missing_parenthesis_is_named_with_its_position(self):
        """Case 16."""
        with self.assertRaises(ParseError) as cm:
            self.parse('select[Age>30](R')
        self.assertIn("')'", str(cm.exception))
        self.assertIn('position 16', str(cm.exception))

    def test_empty_projection_list(self):
        """Case 17."""
        with self.assertRaises(ParseError) as cm:
            self.parse('project[](R)')
        self.assertIn('empty', str(cm.exception))
        self.assertIn('position 8', str(cm.exception))

    def test_other_syntax_errors(self):
        for q in ['select[Age>](R)', 'R S', 'A union', 'select[a=1]', 'project[a,](R)', 'R join B', '(R']:
            with self.subTest(query=q):
                with self.assertRaises(ParseError):
                    self.parse(q)

    def test_keyword_rule(self):
        """Keywords are ordinary names, except not (always a negation) and select, project and
        rename (always operators)."""
        self.parse('select[and=3](R)')
        self.assertEqual(self.tree('union union R').splitlines()[1], '├── Relation(union)')
        with self.assertRaises(ParseError):
            self.parse('select[not=3](R)')
        with self.assertRaises(ParseError):
            self.parse('select union R')
