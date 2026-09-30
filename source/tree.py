"""Parse tree printer. The root is printed bare; children hang from box-drawing connectors."""
from .ast import RelationRef, UnaryOp, BinaryOp, Attr, Literal, Compare

COMPARE_NAMES = {'=': 'Eq', '!=': 'Ne', '<': 'Lt', '<=': 'Le', '>': 'Gt', '>=': 'Ge'}


def attr_text(a):
    return f'{a.relation}.{a.name}' if a.relation else a.name


def cond_text(c):
    if isinstance(c, Attr):
        return f'Attr({attr_text(c)})'
    if isinstance(c, Literal):
        kind = 'Num' if isinstance(c.value, (int, float)) else 'Str'
        return f'{kind}({c.value!r})'
    if isinstance(c, Compare):
        return f'{COMPARE_NAMES[c.op]}({cond_text(c.left)}, {cond_text(c.right)})'
    if c.op == 'not':
        return f'Not({cond_text(c.child)})'
    return f'{c.op.capitalize()}({cond_text(c.left)}, {cond_text(c.right)})'


def label(n):
    if isinstance(n, RelationRef):
        return f'Relation({n.name})'
    if isinstance(n, UnaryOp):
        if n.op == 'select':
            return f'Select(cond={cond_text(n.arg)})'
        if n.op == 'project':
            return f'Project(attrs=[{", ".join(attr_text(a) for a in n.arg)}])'
        return f'Rename(name={n.arg})'
    if n.op == 'join':
        return f'Join(cond={cond_text(n.arg)})'
    return n.op.capitalize()


def children(n):
    if isinstance(n, UnaryOp):
        return [n.child]
    if isinstance(n, BinaryOp):
        return [n.left, n.right]
    return []


def tree(node):
    lines = [label(node)]

    def walk(n, prefix):
        kids = children(n)
        for i, k in enumerate(kids):
            last = i == len(kids) - 1
            lines.append(prefix + ('└── ' if last else '├── ') + label(k))
            walk(k, prefix + ('    ' if last else '│   '))

    walk(node, '')
    return '\n'.join(lines)
