"""The relational operators, schema handling, and the engine's definition of tuple equality.

Operators work on bags. A Relation is a schema plus any iterable of rows, and every operator
returns a Relation whose rows are produced lazily, one at a time, as the caller pulls them. The
operators never ask where the rows come from: a list in memory and a scan of a heap file look the
same to them. Duplicates are kept everywhere except in distinct() and in the set versions of
union, intersect and minus.

Names and types are checked when an operator is called, before any row is pulled.

Nothing in this module prints or touches a file.
"""
import operator
from collections import Counter
from itertools import islice
from dataclasses import dataclass
from .ast import Literal, Logic
from .errors import NameErrorRA, SchemaError, TypeErrorRA

NUMBER = 'number'
STRING = 'string'


# ---------------------------------------------------------------- values and tuple equality

def type_of(value):
    """Every value is a number (int or float) or a string. There are no nulls."""
    return NUMBER if isinstance(value, (int, float)) else STRING


def canonical(value):
    """Canonical form of a value. A float with no fractional part becomes an int, so 30 and 30.0
    are one value. Strings are kept exactly as written: comparison is case-sensitive."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_row(row):
    return tuple(canonical(v) for v in row)


def tuple_equal(a, b):
    """The engine's definition of tuple equality: same arity, and equal canonical values in every
    position. Numbers compare numerically, strings compare exactly."""
    return len(a) == len(b) and all(canonical(x) == canonical(y) for x, y in zip(a, b))


# ---------------------------------------------------------------- schema and relations

@dataclass(frozen=True)
class SchemaAttr:
    relation: str
    name: str
    typ: str            # NUMBER or STRING

    def qualified(self):
        """R.a, or the bare name for a column that belongs to no relation: the output of an
        aggregate, or a column that a projection gave a new name."""
        return f'{self.relation}.{self.name}' if self.relation else self.name


@dataclass
class Relation:
    """A bag of rows with a schema. `rows` is any iterable of canonical tuples. It may be a
    generator that can be walked only once, so an operator that needs a second pass over an
    input copies that input into a list first."""
    name: str
    schema: list        # of SchemaAttr, in output order
    rows: object

    def materialize(self):
        """The same relation with its rows pulled into a list."""
        return Relation(self.name, list(self.schema), list(self.rows))

    def ordered_rows(self):
        return sorted(self.rows, key=repr)


@dataclass
class Counters:
    """Instrumentation, incremented by the operators themselves as rows are pulled.
    select_examined: tuples the selection condition was evaluated on.
    join_compared:   pairs the join condition was evaluated on, whether or not they matched."""
    select_examined: int = 0
    join_compared: int = 0


def attr_text(a):
    return f'{a.relation}.{a.name}' if a.relation else a.name


def operand_text(x):
    return repr(x.value) if isinstance(x, Literal) else attr_text(x)


def resolve_attr(schema, attr):
    """Column index of an attribute reference. A qualified reference R.a matches only column a of
    R. An unqualified name matches any relation; a name that matches more than one column is
    ambiguous and must be qualified."""
    matches = [i for i, a in enumerate(schema)
               if a.name == attr.name and (attr.relation is None or a.relation == attr.relation)]
    if not matches:
        raise NameErrorRA(f"Unknown attribute '{attr_text(attr)}'; the schema is "
                          + ', '.join(a.qualified() for a in schema))
    if len(matches) > 1:
        raise NameErrorRA(f"Ambiguous attribute '{attr.name}'; qualify it as one of "
                          + ', '.join(schema[i].qualified() for i in matches))
    return matches[0]


# ---------------------------------------------------------------- conditions

COMPARE = {'=': operator.eq, '!=': operator.ne, '<>': operator.ne, '<': operator.lt,
           '<=': operator.le, '>': operator.gt, '>=': operator.ge}


def bind_operand(x, schema):
    """Return (getter, type). The getter maps a row to the operand's value."""
    if isinstance(x, Literal):
        value = x.value
        return (lambda row: value), type_of(value)
    i = resolve_attr(schema, x)
    return (lambda row: row[i]), schema[i].typ


def bind(cond, schema):
    """Check a condition against a schema once and return a function row -> bool.

    Every attribute is resolved to a column index and every comparison is type checked here,
    before any row is examined. That is why an unknown attribute or a number-to-string
    comparison is reported even when the input has no rows, or when the comparison sits on the
    right of an 'and' that is never reached. The returned function does no checking, only lookups."""
    if isinstance(cond, Logic):
        if cond.op == 'not':
            inner = bind(cond.child, schema)
            return lambda row: not inner(row)
        left = bind(cond.left, schema)
        right = bind(cond.right, schema)
        if cond.op == 'and':
            return lambda row: left(row) and right(row)
        return lambda row: left(row) or right(row)
    lget, ltype = bind_operand(cond.left, schema)
    rget, rtype = bind_operand(cond.right, schema)
    if ltype != rtype:
        raise TypeErrorRA(f"Cannot compare {ltype} to {rtype} in "
                          f"'{operand_text(cond.left)} {cond.op} {operand_text(cond.right)}'")
    compare = COMPARE[cond.op]
    return lambda row: compare(lget(row), rget(row))


# ---------------------------------------------------------------- unary operators

def select(rel, cond, counters=None):
    test = bind(cond, rel.schema)

    def rows():
        for row in rel.rows:
            if counters is not None:
                counters.select_examined += 1
            if test(row):
                yield row

    return Relation(rel.name, list(rel.schema), rows())


def project(rel, attrs):
    """The listed columns in the listed order. Duplicates are kept: project[DID] over three
    employees gives three rows. Removing them is the job of distinct()."""
    indexes = []
    schema = []
    for item in attrs:                  # an attribute, or (attribute, new name) for SELECT a AS x
        a, alias = item if isinstance(item, tuple) else (item, None)
        i = resolve_attr(rel.schema, a)
        column = SchemaAttr(None, alias, rel.schema[i].typ) if alias else rel.schema[i]
        if column in schema:
            raise SchemaError(f"Duplicate attribute '{column.qualified()}' in projection list")
        indexes.append(i)
        schema.append(column)
    return Relation(rel.name, schema, (tuple(row[i] for i in indexes) for row in rel.rows))


def distinct(rel):
    """Duplicate elimination: the first copy of each row is passed on, later copies are dropped.
    It has to remember every distinct row it has seen, which is why it is not free."""
    def rows():
        seen = set()
        for row in rel.rows:
            if row not in seen:
                seen.add(row)
                yield row

    return Relation(rel.name, list(rel.schema), rows())


def rename(rel, newname):
    schema = [SchemaAttr(newname, a.name, a.typ) for a in rel.schema]
    names = [a.name for a in schema]
    for n in names:
        if names.count(n) > 1:
            raise SchemaError(f"rename[{newname}] would give two attributes the name '{newname}.{n}'")
    return Relation(newname, schema, rel.rows)


# ---------------------------------------------------------------- grouping, sorting, limit
#
# In-memory reference versions. They define what a plan means, so that plans produced by the SQL
# front end can be run and checked. The execution engine has its own versions under a page budget.

AGGREGATES = ('count', 'sum', 'avg', 'min', 'max')


def group_by(rel, keys, aggregates):
    """One output row per distinct combination of the key columns: the keys, then one value per
    aggregate. `aggregates` is a list of (function, attribute or None for *, output name).
    With no keys the whole input is one group, and there is one output row even when the input
    is empty."""
    key_indexes = [resolve_attr(rel.schema, k) for k in keys]
    schema = [rel.schema[i] for i in key_indexes]
    specs = []
    for func, arg, name in aggregates:
        if func not in AGGREGATES:
            raise SchemaError(f"Unknown aggregate '{func}'")
        if arg is None:
            if func != 'count':
                raise SchemaError(f"{func}(*) is not defined; only count accepts *")
            i, typ = None, NUMBER
        else:
            i = resolve_attr(rel.schema, arg)
            if func in ('sum', 'avg') and rel.schema[i].typ != NUMBER:
                raise TypeErrorRA(f"{func}({attr_text(arg)}) needs a number column")
            typ = rel.schema[i].typ if func in ('min', 'max') else NUMBER
        specs.append((func, i, typ))
        schema.append(SchemaAttr(None, name, typ))

    def rows():
        groups = {}                     # key values -> rows of the group, in first-seen order
        for row in rel.rows:
            groups.setdefault(tuple(row[i] for i in key_indexes), []).append(row)
        if not key_indexes and not groups:
            groups[()] = []
        for key, members in groups.items():
            yield key + tuple(_aggregate(func, i, typ, members) for func, i, typ in specs)

    return Relation(rel.name, schema, rows())


def _aggregate(func, i, typ, members):
    if func == 'count':
        return len(members)
    values = [row[i] for row in members]
    if not values:                      # only for a query with no keys over an empty input. There
        return 0 if typ == NUMBER else ''   # are no nulls in this system, so this is a placeholder.
    if func == 'sum':
        return sum(values)
    if func == 'avg':
        return canonical(sum(values) / len(values))
    return min(values) if func == 'min' else max(values)


def sort(rel, keys):
    """keys is a list of (attribute, 'asc' or 'desc'), most significant first."""
    indexes = [(resolve_attr(rel.schema, a), direction) for a, direction in keys]

    def rows():
        data = list(rel.rows)
        for i, direction in reversed(indexes):      # stable sorts, least significant key first
            data.sort(key=lambda row: row[i], reverse=direction == 'desc')
        yield from data

    return Relation(rel.name, list(rel.schema), rows())


def limit(rel, count):
    return Relation(rel.name, list(rel.schema), islice(rel.rows, count))


# ---------------------------------------------------------------- product and join

def product_schema(a, b):
    """Schema of a times b: all attributes of both, qualified. Colliding qualified names are an error."""
    taken = {(x.relation, x.name) for x in a.schema}
    for x in b.schema:
        if (x.relation, x.name) in taken:
            raise SchemaError(f"{a.name} times {b.name} would have two attributes named "
                              f"'{x.qualified()}'; rename one side first")
    return a.schema + b.schema


def times(a, b):
    schema = product_schema(a, b)

    def rows():
        inner = list(b.rows)            # walked once per outer row, so it has to be kept
        for ra in a.rows:
            for rb in inner:
                yield ra + rb

    return Relation(f'{a.name}*{b.name}', schema, rows())


def join(a, b, cond, counters=None):
    """Theta join, defined as times followed by select[cond]: a nested loop that walks the product
    one pair at a time and passes on only the matching pairs. The outer input is streamed; the
    inner input is copied into a list because it is walked once per outer row."""
    schema = product_schema(a, b)
    test = bind(cond, schema)

    def rows():
        inner = list(b.rows)
        for ra in a.rows:
            for rb in inner:
                if counters is not None:
                    counters.join_compared += 1
                pair = ra + rb
                if test(pair):
                    yield pair

    return Relation(f'{a.name}*{b.name}', schema, rows())


# ---------------------------------------------------------------- union, intersect, minus

def union_compatible(a, b):
    """Same number of attributes and the same type position by position, which is SQL's rule.
    Names do not have to match; the result takes the names of the left input.
    Returns None when compatible, otherwise a description of the first mismatch."""
    if len(a.schema) != len(b.schema):
        return f'{a.name} has {len(a.schema)} attributes but {b.name} has {len(b.schema)}'
    for x, y in zip(a.schema, b.schema):
        if x.typ != y.typ:
            return f"attribute '{x.name}' is a {x.typ} in {a.name} but a {y.typ} in {b.name}"
    return None


def setop(a, b, op, keep_all=False):
    """union, intersect and minus, in two versions.

    Set version (keep_all=False), what SQL calls UNION, INTERSECT and EXCEPT: the result has no
    duplicates, whatever the inputs held.

    Bag version (keep_all=True), SQL's UNION ALL, INTERSECT ALL and EXCEPT ALL. A row that occurs
    m times in a and n times in b occurs m + n times in the union, min(m, n) times in the
    intersection and max(m - n, 0) times in the difference."""
    problem = union_compatible(a, b)
    if problem:
        raise SchemaError(f'{a.name} {op} {b.name}: not union compatible ({problem})')
    if keep_all:
        rows = _bag_rows(a.rows, b.rows, op)
    else:
        rows = _set_rows(a.rows, b.rows, op)
    return Relation(a.name, list(a.schema), rows)


def _set_rows(left, right, op):
    seen = set()                        # rows already passed on
    if op == 'union':
        for rows in (left, right):
            for row in rows:
                if row not in seen:
                    seen.add(row)
                    yield row
        return
    other = set(right)
    wanted = op == 'intersect'          # intersect keeps rows found in right, minus keeps the rest
    for row in left:
        if (row in other) == wanted and row not in seen:
            seen.add(row)
            yield row


def _bag_rows(left, right, op):
    if op == 'union':
        yield from left
        yield from right
        return
    remaining = Counter(right)          # copies of each right row not yet paired with a left row
    for row in left:
        paired = remaining[row] > 0
        if paired:
            remaining[row] -= 1
        if paired == (op == 'intersect'):
            yield row
