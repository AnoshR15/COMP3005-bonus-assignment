"""The condition language on its own, for any module that holds a condition as a string.

    cond = parse_condition("R.b = S.b and R.a > 500")     string -> tree
    test = bind(cond, schema)                             tree + schema -> function row -> bool
    test(row)

    test = compile_condition("R.a > 500", schema)         both steps at once
    text = condition_text(cond)                           tree -> string that parses back to it

A schema is a list of SchemaAttr(relation, name, type) in column order, and a row is a tuple in
the same order. bind() resolves every name and checks every comparison's types before it returns,
so the function it returns does lookups only.
"""
from .ast import Literal, Compare
from .operators import bind, attr_text
from .parser import Parser
from .tokenizer import Tokenizer

__all__ = ['parse_condition', 'bind', 'compile_condition', 'condition_text']

# Loosest first. A comparison binds tighter than all three.
PRECEDENCE = {'or': 1, 'and': 2, 'not': 3}
COMPARISON = 4


def parse_condition(text):
    return Parser(Tokenizer(text).scan()).parse_condition()


def compile_condition(text, schema):
    return bind(parse_condition(text), schema)


def condition_text(cond):
    """Write a condition tree as text. parse_condition(condition_text(c)) == c for every tree the
    parser can build, so parentheses are written exactly where the grouping needs them."""
    if isinstance(cond, Compare):
        return f'{_operand(cond.left)} {cond.op} {_operand(cond.right)}'
    if cond.op == 'not':
        return 'not ' + _grouped(cond.child, PRECEDENCE['not'])
    level = PRECEDENCE[cond.op]
    # and, or are left associative: a right operand on the same level was written in parentheses.
    return f'{_grouped(cond.left, level)} {cond.op} {_grouped(cond.right, level + 1)}'


def _grouped(cond, minimum):
    level = COMPARISON if isinstance(cond, Compare) else PRECEDENCE[cond.op]
    text = condition_text(cond)
    return f'({text})' if level < minimum else text


def _operand(x):
    if not isinstance(x, Literal):
        return attr_text(x)
    if isinstance(x.value, str):
        return "'" + x.value.replace("'", "''") + "'"
    text = repr(x.value)
    if 'e' in text:                     # the tokenizer reads plain decimals, not 1e-05
        text = format(x.value, '.20f').rstrip('0')
    return text
