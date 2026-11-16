"""Syntax tree of the SQL subset. Every node carries pos = (line, column) for error messages.

Nothing here knows about the catalog: names are exactly what the query wrote.
"""
from dataclasses import dataclass
from typing import Any

# ---- statements


@dataclass(frozen=True)
class CreateTable:
    name: str
    columns: list           # of (name, 'INT' or 'VARCHAR', size or None, pos)
    pos: tuple


@dataclass(frozen=True)
class Insert:
    table: str
    values: list            # of Lit
    pos: tuple


@dataclass(frozen=True)
class SetOp:
    op: str                 # 'union', 'union_all', 'intersect', 'except'
    left: Any               # Select or SetOp
    right: Any
    pos: tuple


@dataclass(frozen=True)
class Select:
    distinct: bool
    items: Any              # list of SelectItem, or None for SELECT *
    source: Any             # the FROM clause: TableRef, SubqueryRef or FromJoin
    where: Any              # condition or None
    group_by: list          # of Column; empty when there is no GROUP BY
    having: Any             # condition or None
    order_by: list          # of OrderItem
    limit: Any              # int or None
    pos: tuple


@dataclass(frozen=True)
class SelectItem:
    expr: Any               # Column or Aggregate
    alias: Any              # str or None
    pos: tuple


@dataclass(frozen=True)
class OrderItem:
    column: Any             # Column
    descending: bool
    pos: tuple


# ---- FROM clause


@dataclass(frozen=True)
class TableRef:
    name: str
    alias: Any              # str or None
    pos: tuple


@dataclass(frozen=True)
class SubqueryRef:
    query: Any              # Select or SetOp
    alias: str
    pos: tuple


@dataclass(frozen=True)
class FromJoin:
    kind: str               # 'cross' for a comma, 'join' for JOIN ... ON
    left: Any
    right: Any
    condition: Any          # None for 'cross'
    pos: tuple


# ---- operands and conditions


@dataclass(frozen=True)
class Column:
    table: Any              # str or None
    name: str
    pos: tuple


@dataclass(frozen=True)
class Lit:
    value: Any              # int or str
    pos: tuple


@dataclass(frozen=True)
class Aggregate:
    func: str               # 'count', 'sum', 'avg', 'min', 'max'
    arg: Any                # Column, or None for *
    pos: tuple


@dataclass(frozen=True)
class Comparison:
    left: Any
    op: str                 # = <> < <= > >=
    right: Any
    pos: tuple              # of the operator


@dataclass(frozen=True)
class BoolOp:
    op: str                 # 'and', 'or'
    left: Any
    right: Any
    pos: tuple


@dataclass(frozen=True)
class Not:
    child: Any
    pos: tuple
