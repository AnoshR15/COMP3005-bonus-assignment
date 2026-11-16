"""Recursive-descent parser for the SQL subset. Each method is one rule of GRAMMAR_SQL.md.

One token of lookahead, no backtracking. Precedence and associativity live in the grammar: every
left-associative level is  first { operator next }  written as a loop, and each level calls the
next tighter one, so the methods contain no precedence logic of their own.

    conditions   OR  <  AND  <  NOT  <  comparison, ( condition )
    set ops      UNION, UNION ALL, INTERSECT, EXCEPT on one level, left associative
    FROM         comma and JOIN ... ON on one level, left associative
"""
from .errors import SqlError
from .nodes import (CreateTable, Insert, SetOp, Select, SelectItem, OrderItem, TableRef, SubqueryRef,
                    FromJoin, Column, Lit, Aggregate, Comparison, BoolOp, Not)

AGGREGATE_NAMES = ('COUNT', 'SUM', 'AVG', 'MIN', 'MAX')


class Parser:

    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    # ---- token helpers

    def cur(self):
        return self.tokens[self.i]

    def at(self, kind, value=None):
        tok = self.cur()
        return tok.kind == kind and (value is None or tok.value == value)

    def at_keyword(self, *words):
        return self.cur().kind == 'KEYWORD' and self.cur().value in words

    def accept(self, kind, value=None):
        if self.at(kind, value):
            self.i += 1
            return self.tokens[self.i - 1]
        return None

    def keyword(self, word):
        return self.accept('KEYWORD', word)

    def expect(self, kind, value=None, what=None):
        tok = self.accept(kind, value)
        if tok is None:
            self.fail(f"expected {what or value or kind}")
        return tok

    def expect_keyword(self, word):
        return self.expect('KEYWORD', word, what=word)

    def fail(self, message):
        tok = self.cur()
        if tok.kind == 'EOF':
            found = 'end of input'
        elif tok.kind == 'STRING':
            found = f"string '{tok.value}'"
        else:
            found = f"'{tok.value}'"
        raise SqlError('syntax', f'{message}, found {found}', tok.pos)

    def ident(self, what):
        tok = self.cur()
        if tok.kind == 'KEYWORD':
            self.fail(f"expected {what} ('{tok.value}' is a reserved word)")
        return self.expect('IDENT', what=what).value

    # ---- script = [ statement ] { ";" [ statement ] } EOF

    def script(self):
        statements = []
        while not self.at('EOF'):
            if self.accept(';'):
                continue
            statements.append(self.statement())
            if not self.at('EOF'):
                self.expect(';', what="';' or end of input")
        return statements

    # ---- statement = create | insert | query

    def statement(self):
        if self.at_keyword('CREATE'):
            return self.create()
        if self.at_keyword('INSERT'):
            return self.insert()
        if self.at_keyword('SELECT') or self.at('('):
            return self.query()
        self.fail('expected SELECT, CREATE or INSERT')

    # ---- create = CREATE TABLE ident "(" coldef { "," coldef } ")"

    def create(self):
        pos = self.expect_keyword('CREATE').pos
        self.expect_keyword('TABLE')
        name = self.ident('a table name')
        self.expect('(', what="'('")
        columns = [self.coldef()]
        while self.accept(','):
            columns.append(self.coldef())
        self.expect(')', what="',' or ')'")
        return CreateTable(name, columns, pos)

    # ---- coldef = ident ( INT | VARCHAR "(" int ")" )

    def coldef(self):
        pos = self.cur().pos
        name = self.ident('a column name')
        if self.keyword('INT'):
            return (name, 'INT', None, pos)
        if self.keyword('VARCHAR'):
            self.expect('(', what="'(' after VARCHAR")
            size = self.expect('INT', what='the length of the VARCHAR').value
            self.expect(')', what="')'")
            return (name, 'VARCHAR', size, pos)
        self.fail('expected a type, INT or VARCHAR(n)')

    # ---- insert = INSERT INTO ident VALUES "(" literal { "," literal } ")"

    def insert(self):
        pos = self.expect_keyword('INSERT').pos
        self.expect_keyword('INTO')
        table = self.ident('a table name')
        self.expect_keyword('VALUES')
        self.expect('(', what="'('")
        values = [self.literal()]
        while self.accept(','):
            values.append(self.literal())
        self.expect(')', what="',' or ')'")
        return Insert(table, values, pos)

    # ---- literal = int | string

    def literal(self):
        tok = self.accept('INT') or self.accept('STRING')
        if tok is None:
            self.fail('expected a number or a string')
        return Lit(tok.value, tok.pos)

    # ---- query = select_block { setop select_block }
    # ---- setop = UNION [ ALL ] | INTERSECT | EXCEPT

    def query(self):
        left = self.select_block()
        while self.at_keyword('UNION', 'INTERSECT', 'EXCEPT'):
            tok = self.cur()
            self.i += 1
            op = tok.value.lower()
            if op == 'union' and self.keyword('ALL'):
                op = 'union_all'
            right = self.select_block()
            left = SetOp(op, left, right, tok.pos)
        return left

    # ---- select_block = SELECT [ DISTINCT ] select_list FROM from_list [ WHERE condition ]
    #                     [ GROUP BY col_list [ HAVING condition ] ] [ ORDER BY sort_list ] [ LIMIT int ]
    #                   | "(" query ")"

    def select_block(self):
        if self.accept('('):
            inner = self.query()
            self.expect(')', what="')'")
            return inner
        pos = self.expect_keyword('SELECT').pos
        distinct = self.keyword('DISTINCT') is not None
        items = self.select_list()
        self.expect_keyword('FROM')
        source = self.from_list()
        where = self.condition() if self.keyword('WHERE') else None
        group_by, having = [], None
        if self.keyword('GROUP'):
            self.expect_keyword('BY')
            group_by = self.col_list()
            if self.keyword('HAVING'):
                having = self.condition()
        order_by = []
        if self.keyword('ORDER'):
            self.expect_keyword('BY')
            order_by = self.sort_list()
        limit = None
        if self.keyword('LIMIT'):
            tok = self.expect('INT', what='a row count after LIMIT')
            if tok.value < 0:
                raise SqlError('syntax', 'LIMIT cannot be negative', tok.pos)
            limit = tok.value
        return Select(distinct, items, source, where, group_by, having, order_by, limit, pos)

    # ---- select_list = "*" | select_item { "," select_item }

    def select_list(self):
        if self.accept('*'):
            return None
        items = [self.select_item()]
        while self.accept(','):
            items.append(self.select_item())
        return items

    # ---- select_item = ( column | aggregate ) [ AS ident ]

    def select_item(self):
        pos = self.cur().pos
        if self.at_keyword(*AGGREGATE_NAMES):
            expr = self.aggregate()
        elif self.at('IDENT'):
            expr = self.column()
        else:
            self.fail('expected a column or an aggregate')
        alias = self.ident('a name after AS') if self.keyword('AS') else None
        return SelectItem(expr, alias, pos)

    # ---- aggregate = ( COUNT | SUM | AVG | MIN | MAX ) "(" ( column | "*" ) ")"

    def aggregate(self):
        tok = self.cur()
        self.i += 1
        self.expect('(', what=f"'(' after {tok.value}")
        arg = None if self.accept('*') else self.column()
        self.expect(')', what="')'")
        return Aggregate(tok.value.lower(), arg, tok.pos)

    # ---- from_list = table_ref { "," table_ref | JOIN table_ref ON condition }

    def from_list(self):
        left = self.table_ref()
        while True:
            tok = self.cur()
            if self.accept(','):
                left = FromJoin('cross', left, self.table_ref(), None, tok.pos)
            elif self.keyword('JOIN'):
                right = self.table_ref()
                self.expect_keyword('ON')
                left = FromJoin('join', left, right, self.condition(), tok.pos)
            else:
                return left

    # ---- table_ref = ident [ [ AS ] ident ] | "(" query ")" [ AS ] ident

    def table_ref(self):
        pos = self.cur().pos
        if self.accept('('):
            query = self.query()
            self.expect(')', what="')'")
            self.keyword('AS')
            if not self.at('IDENT'):
                self.fail('a subquery in FROM must have an alias; expected a name')
            return SubqueryRef(query, self.ident('an alias'), pos)
        name = self.ident('a table name')
        alias = None
        if self.keyword('AS'):
            alias = self.ident('an alias after AS')
        elif self.at('IDENT'):
            alias = self.ident('an alias')
        return TableRef(name, alias, pos)

    # ---- col_list = column { "," column }

    def col_list(self):
        columns = [self.column()]
        while self.accept(','):
            columns.append(self.column())
        return columns

    # ---- sort_list = sort_item { "," sort_item }        sort_item = column [ ASC | DESC ]

    def sort_list(self):
        items = [self.sort_item()]
        while self.accept(','):
            items.append(self.sort_item())
        return items

    def sort_item(self):
        column = self.column()
        descending = False
        if self.keyword('DESC'):
            descending = True
        else:
            self.keyword('ASC')
        return OrderItem(column, descending, column.pos)

    # ---- condition = or_condition
    # ---- or_condition = and_condition { OR and_condition }

    def condition(self):
        left = self.and_condition()
        while self.at_keyword('OR'):
            pos = self.cur().pos
            self.i += 1
            left = BoolOp('or', left, self.and_condition(), pos)
        return left

    # ---- and_condition = not_condition { AND not_condition }

    def and_condition(self):
        left = self.not_condition()
        while self.at_keyword('AND'):
            pos = self.cur().pos
            self.i += 1
            left = BoolOp('and', left, self.not_condition(), pos)
        return left

    # ---- not_condition = NOT not_condition | "(" condition ")" | comparison

    def not_condition(self):
        tok = self.cur()
        if self.keyword('NOT'):
            return Not(self.not_condition(), tok.pos)
        if self.accept('('):
            inner = self.condition()
            self.expect(')', what="')'")
            return inner
        return self.comparison()

    # ---- comparison = operand ( "=" | "<>" | "<" | "<=" | ">" | ">=" ) operand

    def comparison(self):
        left = self.operand()
        op = self.expect('OP', what='a comparison operator (=, <>, <, <=, >, >=)')
        right = self.operand()
        return Comparison(left, op.value, right, op.pos)

    # ---- operand = column | int | string | aggregate

    def operand(self):
        tok = self.accept('INT') or self.accept('STRING')
        if tok is not None:
            return Lit(tok.value, tok.pos)
        if self.at_keyword(*AGGREGATE_NAMES):
            return self.aggregate()
        if self.at('IDENT'):
            return self.column()
        self.fail('expected a column, a number or a string')

    # ---- column = ident [ "." ident ]

    def column(self):
        pos = self.cur().pos
        first = self.ident('a column name')
        if self.accept('.'):
            return Column(first, self.ident("a column name after '.'"), pos)
        return Column(None, first, pos)


def parse_sql(text):
    """SQL text -> list of statements."""
    from .lexer import scan
    return Parser(scan(text)).script()
