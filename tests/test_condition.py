"""The condition language used on its own: parse a string, bind it to a schema, test a tuple,
and write the tree back as text."""
import unittest
from source.ast import Attr, Literal, Compare, Logic
from source.condition import parse_condition, bind, compile_condition, condition_text
from source.operators import SchemaAttr, NUMBER, STRING
from source.errors import LexError, ParseError, NameErrorRA, TypeErrorRA

# The schema of R join S: R(a number, b number), S(b number, c string).
SCHEMA = [SchemaAttr('R', 'a', NUMBER), SchemaAttr('R', 'b', NUMBER),
          SchemaAttr('S', 'b', NUMBER), SchemaAttr('S', 'c', STRING)]


class ParseConditionTests(unittest.TestCase):

    def test_a_qualified_equality(self):
        self.assertEqual(parse_condition('R.b = S.b'),
                         Compare(Attr('R', 'b'), '=', Attr('S', 'b')))

    def test_precedence_is_the_same_as_inside_a_query(self):
        self.assertEqual(parse_condition('R.a > 500 and R.b = S.b or not S.c = \'x\''),
                         Logic('or',
                               left=Logic('and',
                                          left=Compare(Attr('R', 'a'), '>', Literal(500)),
                                          right=Compare(Attr('R', 'b'), '=', Attr('S', 'b'))),
                               right=Logic('not', child=Compare(Attr('S', 'c'), '=', Literal('x')))))

    def test_errors(self):
        for text, error in [('R.a >', ParseError), ('R.a > 1 )', ParseError), ('', ParseError),
                            ('R.a > 1 R.b', ParseError), ("S.c = 'x", LexError), ('R. = 1', ParseError)]:
            with self.subTest(condition=text):
                with self.assertRaises(error):
                    parse_condition(text)


class EvaluateConditionTests(unittest.TestCase):

    def test_evaluate_against_a_tuple(self):
        test = bind(parse_condition('R.b = S.b and R.a > 500'), SCHEMA)
        self.assertTrue(test((501, 7, 7, 'x')))
        self.assertFalse(test((500, 7, 7, 'x')))
        self.assertFalse(test((501, 7, 8, 'x')))

    def test_compile_does_both_steps(self):
        test = compile_condition("S.c != 'x' or a = 1", SCHEMA)
        self.assertTrue(test((1, 0, 0, 'x')))
        self.assertTrue(test((2, 0, 0, 'y')))
        self.assertFalse(test((2, 0, 0, 'x')))

    def test_unqualified_name_in_two_tables_is_ambiguous(self):
        with self.assertRaises(NameErrorRA) as cm:
            compile_condition('b = 1', SCHEMA)
        self.assertIn('R.b, S.b', str(cm.exception))

    def test_names_and_types_are_checked_before_any_tuple(self):
        with self.assertRaises(NameErrorRA):
            compile_condition('T.a = 1', SCHEMA)          # no table T
        with self.assertRaises(NameErrorRA):
            compile_condition('S.a = 1', SCHEMA)          # a exists, but not in S
        with self.assertRaises(TypeErrorRA):
            compile_condition("R.a = 'five'", SCHEMA)
        with self.assertRaises(TypeErrorRA):
            compile_condition('R.a = S.c', SCHEMA)


class ConditionTextTests(unittest.TestCase):

    def test_text_parses_back_to_the_same_tree(self):
        for text in ['R.b = S.b',
                     'a = 1 and b = 2 or c = 3',
                     'a = 1 and (b = 2 or c = 3)',
                     'a = 1 or b = 2 or c = 3',
                     'a = 1 or (b = 2 or c = 3)',              # same operator, grouped to the right
                     'a = 1 and (b = 2 and c = 3)',
                     'not (a = 1 and b = 2) or c > 3',
                     'not not a = 1',
                     'not a = 1 and not (b = 2 or c = 3)',
                     "Name = 'O''Brien' and Tag != 'a,b)'",
                     'x >= -30 and y <= 2.5 and z < 0.00001']:
            with self.subTest(condition=text):
                tree = parse_condition(text)
                self.assertEqual(parse_condition(condition_text(tree)), tree)

    def test_parentheses_are_written_only_where_needed(self):
        self.assertEqual(condition_text(parse_condition('((a=1) and (b=2)) or (c=3)')),
                         'a = 1 and b = 2 or c = 3')
        self.assertEqual(condition_text(parse_condition('a=1 and (b=2 or c=3)')),
                         'a = 1 and (b = 2 or c = 3)')
