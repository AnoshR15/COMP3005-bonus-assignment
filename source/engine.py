"""Bottom-up evaluation of a plan tree. The tree is executed exactly as written: no rewriting.

The engine does not know where tuples live. It is handed a source, any object with a method

    scan(name) -> Relation        the schema of the named table and an iterable over its rows

and every leaf of the tree asks the source for its rows. MemorySource below serves relations held
in memory; a heap file behind a buffer pool can be passed in the same way.

evaluate() returns a Relation whose rows are produced as they are pulled. Names, schemas and types
of the whole tree are checked before it returns, so an error never waits for the first row.
"""
from .ast import RelationRef, UnaryOp
from .errors import NameErrorRA
from .operators import (Relation, select, project, distinct, rename, times, join, setop,
                        group_by, sort, limit)
from .parser import Parser
from .tokenizer import Tokenizer


class MemorySource:
    """A data source over relations already loaded into memory: {name: Relation}."""

    def __init__(self, relations):
        self.relations = relations

    def scan(self, name):
        if name not in self.relations:
            raise NameErrorRA(f"Unknown relation '{name}'; loaded relations: "
                              + (', '.join(sorted(self.relations)) or 'none'))
        r = self.relations[name]
        return Relation(r.name, list(r.schema), iter(r.rows))


def parse_query(text):
    """Relational algebra text -> plan tree."""
    return Parser(Tokenizer(text).scan()).parse()


def evaluate(node, source, counters=None):
    if isinstance(node, RelationRef):
        return source.scan(node.name)
    if isinstance(node, UnaryOp):
        child = evaluate(node.child, source, counters)
        if node.op == 'select':
            return select(child, node.arg, counters)
        if node.op == 'project':
            return project(child, node.arg)
        if node.op == 'distinct':
            return distinct(child)
        if node.op == 'group_by':
            return group_by(child, *node.arg)
        if node.op == 'sort':
            return sort(child, node.arg)
        if node.op == 'limit':
            return limit(child, node.arg)
        return rename(child, node.arg)
    left = evaluate(node.left, source, counters)
    right = evaluate(node.right, source, counters)
    if node.op in ('union', 'intersect', 'minus'):
        return setop(left, right, node.op, keep_all=node.arg == 'all')
    if node.op == 'times':
        return times(left, right)
    return join(left, right, node.arg, counters)
