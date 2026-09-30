"""Tokenizer: required cases 1 to 9 and the lexical error category."""
from tests.base import RATestCase
from source.errors import LexError


class TokenizerTests(RATestCase):

    def test_no_whitespace_at_all(self):
        """Case 1."""
        self.assertEqual(self.tokens('select[x1=3](R)'), [
            ('WORD', 'select'), ('[', '['), ('WORD', 'x1'), ('OP', '='), ('NUMBER', 3),
            (']', ']'), ('(', '('), ('WORD', 'R'), (')', ')')])

    def test_whitespace_does_not_change_the_tree(self):
        """Case 2."""
        self.assertEqual(self.tree('select[ x1 = 3 ](R)'), self.tree('select[x1=3](R)'))

    def test_greater_or_equal_is_one_token(self):
        """Case 3."""
        ops = [t for t in self.tokens('select[Age>=30](R)') if t[0] == 'OP']
        self.assertEqual(ops, [('OP', '>=')])

    def test_greater_than_followed_by_a_negative_number(self):
        """Case 4."""
        ts = self.tokens('select[Age>-30](R)')
        self.assertIn(('OP', '>'), ts)
        self.assertIn(('NUMBER', -30), ts)
        self.assertNotIn(('OP', '>-'), ts)

    def test_parenthesis_inside_a_string(self):
        """Case 5."""
        self.assertIn(('STRING', 'Bob)'), self.tokens("select[Name='Bob)'](R)"))
        self.assertEqual(self.tree("select[Name='Bob)'](R)").splitlines()[0],
                         "Select(cond=Eq(Attr(Name), Str('Bob)')))")

    def test_comma_inside_a_string(self):
        """Case 6."""
        self.assertIn(('STRING', 'a,b'), self.tokens("select[Name='a,b'](R)"))

    def test_doubled_quote_is_one_quote(self):
        """Case 7."""
        self.assertIn(('STRING', "O'Brien"), self.tokens("select[Name='O''Brien'](R)"))

    def test_keyword_used_as_an_attribute_name(self):
        """Case 8. A keyword is a plain WORD token; GRAMMAR.md section 6 says the parser treats
        it as an attribute wherever an operand is expected."""
        self.assertEqual(self.tokens('select[union=3](R)')[2], ('WORD', 'union'))
        self.assertEqual(self.tree('select[union=3](R)'),
                         'Select(cond=Eq(Attr(union), Num(3)))\n└── Relation(R)')

    def test_unterminated_string_reports_its_position(self):
        """Case 9."""
        with self.assertRaises(LexError) as cm:
            self.parse("select[Name='Bob](R)")
        self.assertIn('position 12', str(cm.exception))

    def test_lexical_errors(self):
        for q in ["select[Name='Bob](R)", 'select[a!3](R)', 'select[a=#](R)', 'select[a=1.](R)']:
            with self.subTest(query=q):
                with self.assertRaises(LexError):
                    self.parse(q)
