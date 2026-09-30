"""Bottom-up evaluation of a parse tree. The tree is executed exactly as written: no rewriting."""
from .ast import RelationRef, UnaryOp
from .errors import NameErrorRA
from .operators import Relation, select, project, rename, times, join, setop


def evaluate(node, relations, counters=None):
    if isinstance(node, RelationRef):
        if node.name not in relations:
            raise NameErrorRA(f"Unknown relation '{node.name}'; loaded relations: "
                              + (', '.join(sorted(relations)) or 'none'))
        r = relations[node.name]
        return Relation(r.name, list(r.schema), set(r.rows))
    if isinstance(node, UnaryOp):
        child = evaluate(node.child, relations, counters)
        if node.op == 'select':
            return select(child, node.arg, counters)
        if node.op == 'project':
            return project(child, node.arg)
        return rename(child, node.arg)
    left = evaluate(node.left, relations, counters)
    right = evaluate(node.right, relations, counters)
    if node.op in ('union', 'intersect', 'minus'):
        return setop(left, right, node.op)
    if node.op == 'times':
        return times(left, right)
    return join(left, right, node.arg, counters)
