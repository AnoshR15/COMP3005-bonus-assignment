from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class RelationRef:
    name: str

@dataclass(frozen=True)
class UnaryOp:
    op: str
    child: Any
    arg: Any = None

@dataclass(frozen=True)
class BinaryOp:
    op: str
    left: Any
    right: Any
    arg: Any = None

@dataclass(frozen=True)
class Attr:
    relation: str | None
    name: str

@dataclass(frozen=True)
class Literal:
    value: Any

@dataclass(frozen=True)
class Compare:
    left: Any
    op: str
    right: Any

@dataclass(frozen=True)
class Logic:
    op: str
    child: Any = None
    left: Any = None
    right: Any = None
