"""Semantic analysis and translation: syntax tree + catalog -> logical plan (JSON-shaped dicts).

The plan of one SELECT block is built bottom to top in a fixed order and nothing is ever moved:

    1 FROM      scan, rename for an alias, join / cross left to right
    2 WHERE     one select with the whole condition
    3 GROUP BY  group_by
    4 HAVING    select above the group_by
    5 SELECT    project, then distinct if DISTINCT was written
    6 ORDER BY  sort
    7 LIMIT     limit
    8 set ops   between whole blocks, left associative

Name resolution happens on the way: every column reference is looked up in the scope built from
FROM and leaves this module as alias.column. The only unqualified names in a plan are names the
query itself created: the output name of an aggregate, or a select-list alias.

Where duplicates are removed: a distinct node is emitted in exactly one place, finish(), and only
when the query wrote DISTINCT. union, intersect and except remove duplicates by their own
definition; group_by produces one row per group. Nothing else removes a duplicate.

Conditions are rebuilt as trees of the Bonus Project 1 engine (Compare, Logic, Attr, Literal)
and written with its condition_text(), so every condition string in a plan is read back by the
same condition parser that wrote it.
"""
from dataclasses import dataclass
from source.ast import Attr, Literal, Compare, Logic
from source.condition import condition_text
from .catalog import ColumnDef
from .errors import SqlError
from .nodes import (CreateTable, Insert, SetOp, TableRef, FromJoin, Lit, Aggregate, BoolOp, Not)
from .parser import parse_sql

INT, VARCHAR = 'INT', 'VARCHAR'


@dataclass(frozen=True)
class Col:
    """A column of a scope entry or of a query's output. table is None for a column the query
    named itself: an aggregate's output or a select-list alias."""
    table: object
    name: str
    kind: str               # INT or VARCHAR

    def text(self):
        return f'{self.table}.{self.name}' if self.table else self.name


def aggregate_sql(agg):
    arg = '*' if agg.arg is None else (f'{agg.arg.table}.{agg.arg.name}' if agg.arg.table else agg.arg.name)
    return f'{agg.func.upper()}({arg})'


def default_aggregate_name(func, arg_text):
    """The output name of an aggregate without AS: count_star, avg_Emp_salary."""
    return func + '_' + ('star' if arg_text == '*' else arg_text.replace('.', '_'))


class Translator:

    def __init__(self, catalog):
        self.catalog = catalog

    # ------------------------------------------------------------ statements

    def statement(self, node):
        if isinstance(node, CreateTable):
            return self.create_table(node)
        if isinstance(node, Insert):
            return self.insert(node)
        plan, _ = self.query(node)
        return plan

    def create_table(self, node):
        if self.catalog.get_table(node.name) is not None:
            raise SqlError('schema', f"table '{node.name}' already exists", node.pos)
        columns = []
        for name, kind, size, pos in node.columns:
            if any(c.name == name for c in columns):
                raise SqlError('schema', f"table '{node.name}' has two columns named '{name}'", pos)
            if kind == VARCHAR and not 1 <= size <= 255:
                raise SqlError('schema', f'VARCHAR({size}) is not allowed; the length is 1 to 255', pos)
            columns.append(ColumnDef(name, kind, size))
        self.catalog.create_table(node.name, columns)        # later statements of the file see it
        return {'op': 'create_table', 'table': node.name,
                'columns': [[c.name, c.type_text()] for c in columns]}

    def insert(self, node):
        columns = self.catalog.get_table(node.table)
        if columns is None:
            raise SqlError('name', f"unknown table '{node.table}'", node.pos)
        if len(node.values) != len(columns):
            raise SqlError('schema', f"table '{node.table}' has {len(columns)} columns "
                                     f'but {len(node.values)} values were given', node.pos)
        for column, value in zip(columns, node.values):
            kind = INT if isinstance(value.value, int) else VARCHAR
            if kind != column.kind:
                raise SqlError('type', f"column '{column.name}' is {column.type_text()} "
                                       f'but the value is {"a number" if kind == INT else "a string"}', value.pos)
            if kind == VARCHAR and len(value.value.encode('utf-8')) > column.size:
                raise SqlError('type', f"the value is {len(value.value.encode('utf-8'))} bytes long but column "
                                       f"'{column.name}' is {column.type_text()}", value.pos)
        return {'op': 'insert', 'table': node.table, 'values': [v.value for v in node.values]}

    # ------------------------------------------------------------ queries

    def query(self, node):
        """-> (plan, output columns)"""
        if isinstance(node, SetOp):
            return self.set_op(node)
        return self.select(node)

    def set_op(self, node):
        left, left_cols = self.query(node.left)
        right, right_cols = self.query(node.right)
        name = node.op.upper().replace('_', ' ')
        if len(left_cols) != len(right_cols):
            raise SqlError('schema', f'{name}: the left query has {len(left_cols)} columns and '
                                     f'the right query has {len(right_cols)}', node.pos)
        for i, (a, b) in enumerate(zip(left_cols, right_cols), start=1):
            if a.kind != b.kind:
                raise SqlError('schema', f'{name}: column {i} is {a.kind} ({a.text()}) on the left '
                                         f'and {b.kind} ({b.text()}) on the right', node.pos)
        return {'op': node.op, 'left': left, 'right': right}, left_cols

    def select(self, s):
        scope = []                                      # list of (key, [Col]) in FROM order
        plan = self.source(s.source, scope)             # 1 FROM
        if s.where is not None:                         # 2 WHERE
            plan = {'op': 'select', 'condition': self.condition(s.where, self.plain_operand(scope, 'WHERE')),
                    'child': plan}
        aggregating = bool(s.group_by) or (
            s.items is not None and any(isinstance(item.expr, Aggregate) for item in s.items))
        if aggregating:
            plan, items, keys = self.grouping(s, plan, scope)       # 3 GROUP BY, 4 HAVING
        else:
            keys = None
            if s.items is None:                         # SELECT *: every column, qualified
                items = [(c.text(), c, c) for _, cols in scope for c in cols]
            else:
                items = [self.column_item(self.resolve(item.expr, scope), item.alias) for item in s.items]
        return self.finish(s, plan, scope, items, keys)             # 5 SELECT, 6 ORDER BY, 7 LIMIT

    # ------------------------------------------------------------ 1 FROM

    def source(self, node, scope):
        """Translate the FROM tree left to right, adding one scope entry per table reference."""
        if isinstance(node, FromJoin):
            left = self.source(node.left, scope)
            right = self.source(node.right, scope)
            if node.kind == 'cross':
                return {'op': 'cross', 'left': left, 'right': right}
            return {'op': 'join', 'condition': self.condition(node.condition, self.plain_operand(scope, 'ON')),
                    'left': left, 'right': right}
        if isinstance(node, TableRef):
            columns = self.catalog.get_table(node.name)
            if columns is None:
                raise SqlError('name', f"unknown table '{node.name}'", node.pos)
            key = node.alias or node.name
            plan = {'op': 'scan', 'table': node.name}
            if node.alias:
                plan = {'op': 'rename', 'to': key, 'child': plan}
            entry = [Col(key, c.name, c.kind) for c in columns]
        else:                                           # a subquery: translated alone, then renamed
            key = node.alias
            inner, output = self.query(node.query)
            names = [c.name for c in output]
            for name in names:
                if names.count(name) > 1:
                    raise SqlError('schema', f"subquery '{key}' has two columns named '{name}'; "
                                             'give one of them a different name with AS', node.pos)
            plan = {'op': 'rename', 'to': key, 'child': inner}
            entry = [Col(key, c.name, c.kind) for c in output]
        if any(key == other for other, _ in scope):
            raise SqlError('name', f"duplicate alias '{key}' in FROM; "
                                   'give one of the two a different alias', node.pos)
        scope.append((key, entry))
        return plan

    def resolve(self, column, scope):
        """Column reference -> the one Col it names."""
        if column.table is not None:
            for key, cols in scope:
                if key == column.table:
                    for c in cols:
                        if c.name == column.name:
                            return c
                    raise SqlError('name', f"unknown column '{column.name}' in '{key}'; its columns are "
                                           + ', '.join(c.name for c in cols), column.pos)
            raise SqlError('name', f"unknown table or alias '{column.table}' in "
                                   f"'{column.table}.{column.name}'; FROM has "
                                   + ', '.join(key for key, _ in scope), column.pos)
        found = [c for _, cols in scope for c in cols if c.name == column.name]
        if not found:
            raise SqlError('name', f"unknown column '{column.name}'", column.pos)
        if len(found) > 1:
            raise SqlError('ambiguity', f"column '{column.name}' is ambiguous; it could be "
                                        + ' or '.join(c.text() for c in found), column.pos)
        return found[0]

    # ------------------------------------------------------------ conditions

    def condition(self, node, operand):
        """Condition syntax tree -> condition string with every name resolved.
        `operand` maps one operand to (engine node, type); it differs between clauses."""
        return condition_text(self.lower(node, operand))

    def lower(self, node, operand):
        if isinstance(node, BoolOp):
            return Logic(node.op, left=self.lower(node.left, operand), right=self.lower(node.right, operand))
        if isinstance(node, Not):
            return Logic('not', child=self.lower(node.child, operand))
        left, left_kind = operand(node.left)
        right, right_kind = operand(node.right)
        if left_kind != right_kind:
            text = condition_text(Compare(left, node.op, right))
            raise SqlError('type', f"cannot compare {left_kind} with {right_kind} in '{text}'", node.pos)
        return Compare(left, node.op, right)

    @staticmethod
    def literal(lit):
        return Literal(lit.value), INT if isinstance(lit.value, int) else VARCHAR

    def plain_operand(self, scope, clause):
        """Operands of WHERE and ON: columns and literals. An aggregate is an error there."""
        def operand(x):
            if isinstance(x, Lit):
                return self.literal(x)
            if isinstance(x, Aggregate):
                raise SqlError('aggregate', f'{aggregate_sql(x)} cannot be used in {clause}; '
                                            f'{clause} is evaluated before grouping', x.pos)
            col = self.resolve(x, scope)
            return Attr(col.table, col.name), col.kind
        return operand

    # ------------------------------------------------------------ 3 GROUP BY, 4 HAVING

    def grouping(self, s, plan, scope):
        """-> (plan with group_by and HAVING on top, select items, grouping columns)"""
        keys = []
        for column in s.group_by:
            col = self.resolve(column, scope)
            if col not in keys:
                keys.append(col)
        aggregates = []                                 # [function, argument, output name, type]

        def use(agg, alias):
            """The output name and type of an aggregate, adding it to the group_by if it is new."""
            if agg.arg is None:
                if agg.func != 'count':
                    raise SqlError('aggregate', f'{aggregate_sql(agg)} is not defined; only COUNT accepts *', agg.pos)
                arg, kind = '*', INT
            else:
                col = self.resolve(agg.arg, scope)
                if agg.func in ('sum', 'avg') and col.kind != INT:
                    raise SqlError('type', f'{agg.func.upper()} needs an INT column; {col.text()} is VARCHAR',
                                   agg.arg.pos)
                arg = col.text()
                kind = col.kind if agg.func in ('min', 'max') else INT
            same = [a for a in aggregates if a[0] == agg.func and a[1] == arg]
            if alias is None and same:
                return same[0][2], kind                 # e.g. HAVING COUNT(*) reuses COUNT(*) AS n
            name = alias or default_aggregate_name(agg.func, arg)
            for a in aggregates:
                if a[2] == name:
                    if a in same:
                        return name, kind
                    raise SqlError('schema', f"two different aggregates are both named '{name}'", agg.pos)
            aggregates.append([agg.func, arg, name, kind])
            return name, kind

        if s.items is None:
            raise SqlError('aggregate', 'SELECT * cannot be used with GROUP BY; list the grouping '
                                        'columns and aggregates', s.pos)
        items = []
        for item in s.items:
            if isinstance(item.expr, Aggregate):
                name, kind = use(item.expr, item.alias)
                items.append((name, Col(None, name, kind), None))
            else:
                col = self.resolve(item.expr, scope)
                if col not in keys:
                    raise SqlError('aggregate', f"column '{col.text()}' must appear in GROUP BY or be "
                                                'inside an aggregate', item.expr.pos)
                items.append(self.column_item(col, item.alias))

        def having_operand(x):
            if isinstance(x, Lit):
                return self.literal(x)
            if isinstance(x, Aggregate):
                name, kind = use(x, None)
                return Attr(None, name), kind
            col = self.resolve(x, scope)
            if col not in keys:
                raise SqlError('aggregate', f"HAVING may use only grouping columns and aggregates; "
                                            f"'{col.text()}' is not in GROUP BY", x.pos)
            return Attr(col.table, col.name), col.kind

        having = self.condition(s.having, having_operand) if s.having is not None else None
        plan = {'op': 'group_by', 'keys': [k.text() for k in keys],
                'aggregates': [a[:3] for a in aggregates], 'child': plan}
        if having is not None:
            plan = {'op': 'select', 'condition': having, 'child': plan}
        return plan, items, keys

    # ------------------------------------------------------------ 5 SELECT, 6 ORDER BY, 7 LIMIT

    @staticmethod
    def column_item(col, alias):
        """-> (entry of the project's column list, output column, source column)"""
        if alias:
            return [col.text(), alias], Col(None, alias, col.kind), col
        return col.text(), col, col

    def finish(self, s, plan, scope, items, keys):
        output = [out for _, out, _ in items]
        aliases = {out.name for out in output if out.table is None}
        output_name = {}                                # source column -> its name in the output
        for _, out, src in items:
            if src is not None:
                output_name.setdefault(src, out.text())

        sort_keys = []
        extra = []                      # ORDER BY columns the select list does not contain
        for item in s.order_by:
            column = item.column
            if column.table is None and column.name in aliases:     # output aliases first
                key = column.name
            else:                                                   # then the FROM scope
                col = self.resolve(column, scope)
                if keys is not None and col not in keys:
                    raise SqlError('aggregate', f"ORDER BY may use only grouping columns and output names "
                                                f"here; '{col.text()}' is not in GROUP BY", column.pos)
                if col in output_name:
                    key = output_name[col]
                elif s.distinct:
                    raise SqlError('schema', f"with SELECT DISTINCT, the ORDER BY column '{col.text()}' "
                                             'must be in the select list', column.pos)
                else:
                    if col not in extra:
                        extra.append(col)
                    key = col.text()
            sort_keys.append([key, 'desc' if item.descending else 'asc'])

        plan = {'op': 'project', 'columns': [entry for entry, _, _ in items] + [c.text() for c in extra],
                'child': plan}
        # DISTINCT is the one place a distinct node comes from. Above a group_by whose keys are
        # all in the select list the rows are already different from each other, so it is left out.
        grouped_rows_are_unique = keys is not None and all(k in output_name for k in keys)
        if s.distinct and not grouped_rows_are_unique:
            plan = {'op': 'distinct', 'child': plan}
        if sort_keys:
            plan = {'op': 'sort', 'keys': sort_keys, 'child': plan}
        if extra:                       # the sort needed them; the query did not ask for them
            plan = {'op': 'project', 'columns': [out.text() for out in output], 'child': plan}
        if s.limit is not None:
            plan = {'op': 'limit', 'count': s.limit, 'child': plan}
        return plan, output


def compile_sql(text, catalog):
    """SQL text -> one logical plan (a dict ready for json.dumps) per statement."""
    translator = Translator(catalog)
    return [translator.statement(node) for node in parse_sql(text)]
