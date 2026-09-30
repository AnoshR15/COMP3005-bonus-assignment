"""The six relational operators, schema handling, and the engine's definition of tuple equality.

Rows are plain Python tuples kept in canonical form (see canonical()). A Relation stores its rows
in a set. Because every stored row is canonical, two rows are duplicates exactly when tuple_equal()
says they are, and the set container only ever has to test "is this the identical tuple".
"""
import operator
from dataclasses import dataclass
from .ast import Attr, Literal, Compare, Logic
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


def dedup(rows):
    """Set semantics: keep one representative of each tuple_equal class.
    Every row is stored in canonical form, so two rows are tuple_equal exactly when their
    canonical tuples are identical, and 'identical tuple' is the only test the set performs."""
    return {canonical_row(r) for r in rows}


# ---------------------------------------------------------------- schema and relations

@dataclass(frozen=True)
class SchemaAttr:
    relation: str
    name: str
    typ: str            # NUMBER or STRING

    def qualified(self):
        return f'{self.relation}.{self.name}'


@dataclass
class Relation:
    name: str
    schema: list        # of SchemaAttr, in output order
    rows: set           # of canonical tuples

    def ordered_rows(self):
        return sorted(self.rows, key=repr)


@dataclass
class Counters:
    """Instrumentation, incremented by the operators themselves.
    select_examined: tuples the selection condition was evaluated on.
    join_compared:   pairs the join condition was evaluated on, whether or not they matched."""
    select_examined: int = 0
    join_compared: int = 0


def attr_text(a):
    return f'{a.relation}.{a.name}' if a.relation else a.name


def operand_text(x):
    return repr(x.value) if isinstance(x, Literal) else attr_text(x)


def resolve_attr(schema, attr):
    """Column index of an attribute reference. Unqualified names match any relation; a name that
    matches more than one column is ambiguous and must be qualified."""
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

COMPARE = {'=': operator.eq, '!=': operator.ne, '<': operator.lt,
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


# ---------------------------------------------------------------- the six operators

def select(rel, cond, counters=None):
    test = bind(cond, rel.schema)
    out = set()
    examined = 0
    for row in rel.rows:
        examined += 1
        if test(row):
            out.add(row)
    if counters is not None:
        counters.select_examined += examined
    return Relation(rel.name, list(rel.schema), out)


def project(rel, attrs):
    indexes = []
    for a in attrs:
        i = resolve_attr(rel.schema, a)
        if i in indexes:
            raise SchemaError(f"Duplicate attribute '{attr_text(a)}' in projection list")
        indexes.append(i)
    schema = [rel.schema[i] for i in indexes]
    return Relation(rel.name, schema, dedup(tuple(row[i] for i in indexes) for row in rel.rows))


def rename(rel, newname):
    schema = [SchemaAttr(newname, a.name, a.typ) for a in rel.schema]
    names = [a.name for a in schema]
    for n in names:
        if names.count(n) > 1:
            raise SchemaError(f"rename[{newname}] would give two attributes the name '{newname}.{n}'")
    return Relation(newname, schema, set(rel.rows))


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
    return Relation(f'{a.name}*{b.name}', schema, {ra + rb for ra in a.rows for rb in b.rows})


def join(a, b, cond, counters=None):
    """Theta join, defined as times followed by select[cond].

    The product is walked one pair at a time and only matching pairs are kept, so the n*m
    intermediate relation is never stored. The result equals select(times(a, b), cond)."""
    schema = product_schema(a, b)
    test = bind(cond, schema)
    out = set()
    compared = 0
    for ra in a.rows:
        for rb in b.rows:
            compared += 1
            pair = ra + rb
            if test(pair):
                out.add(pair)
    if counters is not None:
        counters.join_compared += compared
    return Relation(f'{a.name}*{b.name}', schema, out)


def union_compatible(a, b):
    """Same number of attributes, same names in the same order, same type position by position.
    Returns None when compatible, otherwise a description of the first mismatch."""
    if len(a.schema) != len(b.schema):
        return f'{a.name} has {len(a.schema)} attributes but {b.name} has {len(b.schema)}'
    for x, y in zip(a.schema, b.schema):
        if x.name != y.name:
            return f"attribute '{x.name}' of {a.name} lines up with '{y.name}' of {b.name}"
        if x.typ != y.typ:
            return f"attribute '{x.name}' is a {x.typ} in {a.name} but a {y.typ} in {b.name}"
    return None


def setop(a, b, op):
    problem = union_compatible(a, b)
    if problem:
        raise SchemaError(f'{a.name} {op} {b.name}: not union compatible ({problem})')
    if op == 'union':
        rows = a.rows | b.rows
    elif op == 'intersect':
        rows = a.rows & b.rows
    else:
        rows = a.rows - b.rows
    return Relation(a.name, list(a.schema), rows)
