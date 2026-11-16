"""Recursive-descent parser. Each method is one rule of the grammar in GRAMMAR.md.

Expression precedence, loosest to tightest:
    set_expr      union | intersect | minus     left associative
    product_expr  times                         left associative
    join_expr     join[cond]                    left associative
    primary       select[..](..), project[..](..), rename[..](..), distinct(..), ( expr ), relation name

Condition precedence, loosest to tightest:   or  <  and  <  not  <  comparison, ( condition )

Every left-associative level is written as   first { op next }   with a loop, never as
level = level op next, because that left recursion would call itself forever.
Keywords are ordinary WORD tokens; the parser decides from context whether a word is an operator.

The parser takes a list of tokens and returns a tree. It does not import the tokenizer: parse() reads
a whole query, parse_condition() reads a condition on its own, such as  R.b = S.b and R.a > 500.
"""
from .ast import RelationRef, UnaryOp, BinaryOp, Attr, Literal, Compare, Logic
from .errors import ParseError

SET_OPS = ('union', 'intersect', 'minus')


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    # ---- token helpers
    def cur(self):
        return self.tokens[self.i]

    def accept(self, kind, value=None):
        """Consume and return the current token if it matches, otherwise return None."""
        tok = self.cur()
        if tok.kind == kind and (value is None or tok.value == value):
            self.i += 1
            return tok
        return None

    def expect(self, kind, value=None, what=None):
        tok = self.accept(kind, value)
        if tok is None:
            raise ParseError(f'Expected {what or value or kind} {self.where()}')
        return tok

    def word(self, value):
        return self.accept('WORD', value)

    def where(self):
        tok = self.cur()
        if tok.kind == 'EOF':
            return f'at end of query (position {tok.pos})'
        return f"at position {tok.pos}, found '{tok.value}'"

    # ---- query = expr EOF
    def parse(self):
        e = self.expr()
        self.expect('EOF', what='end of query')
        return e

    # ---- a condition on its own = condition EOF
    def parse_condition(self):
        c = self.condition()
        self.expect('EOF', what='end of condition')
        return c

    # ---- expr = set_expr
    def expr(self):
        return self.set_expr()

    # ---- set_expr = product_expr { ("union" | "intersect" | "minus") product_expr }
    def set_expr(self):
        left = self.product_expr()
        while self.cur().kind == 'WORD' and self.cur().value in SET_OPS:
            op = self.cur().value
            self.i += 1
            right = self.product_expr()
            left = BinaryOp(op, left, right)
        return left

    # ---- product_expr = join_expr { "times" join_expr }
    def product_expr(self):
        left = self.join_expr()
        while self.word('times'):
            right = self.join_expr()
            left = BinaryOp('times', left, right)
        return left

    # ---- join_expr = primary { "join" "[" condition "]" primary }
    def join_expr(self):
        left = self.primary()
        while self.word('join'):
            self.expect('[', what="'['")
            cond = self.condition()
            self.expect(']', what="']'")
            right = self.primary()
            left = BinaryOp('join', left, right, cond)
        return left

    # ---- primary = "(" expr ")" | select | project | rename | distinct | relation
    def primary(self):
        if self.accept('('):
            e = self.expr()
            self.expect(')', what="')'")
            return e
        if self.word('select'):
            self.expect('[', what="'['")
            cond = self.condition()
            self.expect(']', what="']'")
            return UnaryOp('select', self.parenthesized_expr(), cond)
        if self.word('project'):
            self.expect('[', what="'['")
            if self.cur().kind == ']':
                raise ParseError(f'Projection attribute list cannot be empty {self.where()}')
            attrs = [self.attr()]
            while self.accept(','):
                attrs.append(self.attr())
            self.expect(']', what="']'")
            return UnaryOp('project', self.parenthesized_expr(), attrs)
        if self.word('rename'):
            self.expect('[', what="'['")
            name = self.expect('WORD', what='the new relation name').value
            self.expect(']', what="']'")
            return UnaryOp('rename', self.parenthesized_expr(), name)
        if self.word('distinct'):
            return UnaryOp('distinct', self.parenthesized_expr())
        tok = self.expect('WORD', what="a relation name, select, project, rename, distinct or '('")
        return RelationRef(tok.value)

    def parenthesized_expr(self):
        """The ( expr ) input of a unary operator."""
        self.expect('(', what="'('")
        e = self.expr()
        self.expect(')', what="')'")
        return e

    # ---- condition = or_condition
    def condition(self):
        return self.or_condition()

    # ---- or_condition = and_condition { "or" and_condition }
    def or_condition(self):
        left = self.and_condition()
        while self.word('or'):
            left = Logic('or', left=left, right=self.and_condition())
        return left

    # ---- and_condition = not_condition { "and" not_condition }
    def and_condition(self):
        left = self.not_condition()
        while self.word('and'):
            left = Logic('and', left=left, right=self.not_condition())
        return left

    # ---- not_condition = "not" not_condition | "(" condition ")" | comparison
    def not_condition(self):
        if self.word('not'):
            return Logic('not', child=self.not_condition())
        if self.accept('('):
            c = self.condition()
            self.expect(')', what="')'")
            return c
        return self.comparison()

    # ---- comparison = operand COMP_OP operand
    def comparison(self):
        left = self.operand()
        op = self.expect('OP', what='a comparison operator (=, !=, <>, <, <=, >, >=)').value
        right = self.operand()
        return Compare(left, op, right)

    # ---- operand = NUMBER | STRING | attr
    def operand(self):
        tok = self.accept('NUMBER') or self.accept('STRING')
        if tok is not None:
            return Literal(tok.value)
        if self.cur().kind != 'WORD':
            raise ParseError(f'Expected a number, string or attribute name {self.where()}')
        return self.attr()

    # ---- attr = IDENT [ "." IDENT ]
    def attr(self):
        first = self.expect('WORD', what='an attribute name').value
        if self.accept('.'):
            return Attr(first, self.expect('WORD', what='an attribute name after the dot').value)
        return Attr(None, first)

