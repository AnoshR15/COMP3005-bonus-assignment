"""The seven rewrite rules, applied to a logical plan before anything is costed.

    1  split conjunctions       select[p AND q] becomes two selects. Always safe.
    2  push a select below a join or cross, to the side whose columns are all it references.
    3  cross + select = join    when the predicate references both sides and is an equality.
                                The same predicate above a join is added to the join's condition.
    4  push a select below a rename, union or union_all. Through a rename the column names are
       rewritten; below a union the predicate goes into both branches, renamed for the right one.
       A rename directly over a scan is left alone: there is nothing below it to reach.
       The same rule takes a select through a projection, which is a rename that also drops
       columns: a column given a new name is rewritten to its source. Without that, a predicate
       on a subquery in FROM could never get past the subquery's own select list.
    5  push a select below a group_by, only when it references grouping columns alone.
    6  push projections down    below a join, cross, group_by or sort, an input that carries
                                columns nobody above needs gets a projection keeping the rest.
    7  tidy up                  drop a projection that drops nothing; merge adjacent selects.

A rule applied where its precondition fails gives wrong answers, not slow ones, so every
precondition is checked on column names resolved against the schema of the node in question.

rewrite(plan, catalog, disabled) returns a new plan; the input is not changed. `disabled` is a
set of rule numbers to leave out, which is how REPORT_M4.md measures what each rule is worth.

Order. Rules 1 to 5 are one pass: every select is split, and each piece is sunk as far down as
its preconditions allow. Then rule 6, because a projection would stop a select from sinking
further. Then rule 7.
"""
from source.ast import Attr, Literal, Compare, Logic
from source.condition import parse_condition, condition_text
from .estimate import conjuncts

SET_OPS = ('union', 'union_all', 'intersect', 'except')
ALL_RULES = (1, 2, 3, 4, 5, 6, 7)


class RewriteError(Exception):
    pass


# ------------------------------------------------------------------ schemas and names

def output_columns(node, catalog):
    """The column names a logical node produces, in order."""
    op = node['op']
    if op == 'scan':
        info = catalog.get_table(node['table'])
        if info is None:
            raise RewriteError(f"unknown table '{node['table']}'")
        return [f"{node['table']}.{name}" for name, _, _ in info.columns]
    if op == 'rename':
        return [f"{node['to']}.{c.rpartition('.')[2]}" for c in output_columns(node['child'], catalog)]
    if op == 'project':
        return [c[1] if isinstance(c, list) else c for c in node['columns']]
    if op in ('join', 'cross'):
        return output_columns(node['left'], catalog) + output_columns(node['right'], catalog)
    if op == 'group_by':
        return list(node['keys']) + [a[2] for a in node['aggregates']]
    if op in SET_OPS:
        return output_columns(node['left'], catalog)
    return output_columns(node['child'], catalog)       # select, distinct, sort, limit


def resolve(columns, ref):
    """The column a reference names: "e.did" exactly, or a bare name that fits one column only."""
    text = ref if isinstance(ref, str) else (f'{ref.relation}.{ref.name}' if ref.relation else ref.name)
    if text in columns:
        return text
    if '.' not in text:
        hits = [c for c in columns if c.endswith('.' + text)]
        if len(hits) == 1:
            return hits[0]
    return None


def attributes(tree):
    """Every column reference in a condition."""
    if isinstance(tree, Logic):
        if tree.op == 'not':
            return attributes(tree.child)
        return attributes(tree.left) + attributes(tree.right)
    return [x for x in (tree.left, tree.right) if isinstance(x, Attr)]


def renamed(tree, mapping):
    """The same condition with column names replaced: mapping is {old name: new name}."""
    if isinstance(tree, Logic):
        if tree.op == 'not':
            return Logic('not', child=renamed(tree.child, mapping))
        return Logic(tree.op, left=renamed(tree.left, mapping), right=renamed(tree.right, mapping))

    def swap(x):
        if not isinstance(x, Attr):
            return x
        relation, _, name = mapping[x].rpartition('.')
        return Attr(relation or None, name)
    return Compare(swap(tree.left), tree.op, swap(tree.right))


def select(tree, child):
    return {'op': 'select', 'condition': condition_text(tree), 'child': child}


# ------------------------------------------------------------------ rules 1 to 5

class Sinker:

    def __init__(self, catalog, disabled):
        self.catalog = catalog
        self.on = lambda rule: rule not in disabled

    def walk(self, node):
        """Rewrite the inputs first, then deal with this node if it is a select."""
        node = dict(node)
        for field in ('child', 'left', 'right'):
            if field in node:
                node[field] = self.walk(node[field])
        if node['op'] != 'select':
            return node
        tree = parse_condition(node['condition'])
        pieces = conjuncts(tree) if self.on(1) else [tree]              # rule 1
        below = node['child']
        for piece in pieces:
            below = self.sink(piece, below)
        return below

    def sink(self, pred, node):
        """Put one predicate as far down the tree under `node` as the rules allow."""
        op = node['op']
        refs = attributes(pred)
        if op == 'select':                          # selects commute: keep going down
            return {**node, 'child': self.sink(pred, node['child'])}
        if op in ('join', 'cross'):
            left = output_columns(node['left'], self.catalog)
            right = output_columns(node['right'], self.catalog)
            in_left = [resolve(left, r) is not None for r in refs]
            in_right = [resolve(right, r) is not None for r in refs]
            if refs and all(in_left) and not any(in_right) and self.on(2):          # rule 2
                return {**node, 'left': self.sink(pred, node['left'])}
            if refs and all(in_right) and not any(in_left) and self.on(2):
                return {**node, 'right': self.sink(pred, node['right'])}
            if self.on(3) and self.joins_the_sides(pred, left, right):              # rule 3
                if op == 'cross':
                    return {'op': 'join', 'condition': condition_text(pred),
                            'left': node['left'], 'right': node['right']}
                both = Logic('and', left=parse_condition(node['condition']), right=pred)
                return {**node, 'condition': condition_text(both)}
        if op == 'rename' and self.on(4) and node['child']['op'] != 'scan':         # rule 4
            outer = output_columns(node, self.catalog)
            inner = output_columns(node['child'], self.catalog)
            names = [resolve(outer, r) for r in refs]
            if all(n is not None for n in names):
                mapping = {r: inner[outer.index(n)] for r, n in zip(refs, names)}
                return {**node, 'child': self.sink(renamed(pred, mapping), node['child'])}
        if op == 'project' and self.on(4):
            outputs = [c[1] if isinstance(c, list) else c for c in node['columns']]
            sources = [c[0] if isinstance(c, list) else c for c in node['columns']]
            inner = output_columns(node['child'], self.catalog)
            names = [resolve(outputs, r) for r in refs]
            if all(n is not None for n in names):
                mapping = {r: resolve(inner, sources[outputs.index(n)]) for r, n in zip(refs, names)}
                if all(m is not None for m in mapping.values()):
                    return {**node, 'child': self.sink(renamed(pred, mapping), node['child'])}
        if op in ('union', 'union_all') and self.on(4):
            left = output_columns(node['left'], self.catalog)
            right = output_columns(node['right'], self.catalog)
            names = [resolve(left, r) for r in refs]
            if all(n is not None for n in names):
                to_left = {r: n for r, n in zip(refs, names)}
                to_right = {r: right[left.index(n)] for r, n in zip(refs, names)}
                return {**node, 'left': self.sink(renamed(pred, to_left), node['left']),
                        'right': self.sink(renamed(pred, to_right), node['right'])}
        if op == 'group_by' and self.on(5):                                         # rule 5
            keys = list(node['keys'])
            if refs and all(resolve(keys, r) is not None for r in refs):
                return {**node, 'child': self.sink(pred, node['child'])}
        return select(pred, node)

    @staticmethod
    def joins_the_sides(pred, left, right):
        """An equality between a column of the left input and a column of the right input."""
        if not (isinstance(pred, Compare) and pred.op == '=' and isinstance(pred.left, Attr)
                and isinstance(pred.right, Attr)):
            return False
        a_left, a_right = resolve(left, pred.left), resolve(right, pred.left)
        b_left, b_right = resolve(left, pred.right), resolve(right, pred.right)
        return bool((a_left and b_right and not a_right and not b_left)
                    or (a_right and b_left and not a_left and not b_right))


# ------------------------------------------------------------------ rule 6

def in_order(*groups):
    """The names of several sequences as one list: each once, in the order first met, no None."""
    seen, out = set(), []
    for group in groups:
        for name in group:
            if name is not None and name not in seen:
                seen.add(name)
                out.append(name)
    return out


class Projector:
    """Rule 6. `required` is always a list, in a meaningful order: first the columns the
    ancestors need, in the order they need them, then the columns the operator needs itself.
    A projection that is inserted lists its columns in that order, which is the order of the
    rule's own sentence and of the specification's example, project [e.name, e.did]: e.name for
    the select list above, e.did for the join. It does not depend on the order of the table's
    columns."""

    def __init__(self, catalog):
        self.catalog = catalog

    def cols(self, node):
        return output_columns(node, self.catalog)

    def needs(self, columns, condition):
        return in_order(resolve(columns, r) for r in attributes(parse_condition(condition)))

    def narrow(self, node, required):
        """`node`, rewritten, under a projection if it produces columns that are not required.
        A join or cross is never wrapped here: what a join result should carry is decided when
        the join order is known. An existing projection is trimmed, not wrapped."""
        node = self.walk(node, required)
        columns = self.cols(node)
        if node['op'] in ('join', 'cross') or all(c in required for c in columns):
            return node
        if node['op'] == 'project':
            keep = [c for c in node['columns'] if (c[1] if isinstance(c, list) else c) in required]
            return {**node, 'columns': keep or node['columns'][:1]}
        # A projection cannot be empty: COUNT(*) needs rows, even rows of one column.
        return {'op': 'project', 'columns': [c for c in required if c in columns] or columns[:1], 'child': node}

    def walk(self, node, required):
        """Rewrite the tree under `node`, given the columns of its output that are needed above."""
        op = node['op']
        if op == 'scan':
            return node
        if op in ('join', 'cross'):
            left, right = self.cols(node['left']), self.cols(node['right'])
            need = in_order(required, self.needs(left + right, node['condition']) if op == 'join' else ())
            return {**node, 'left': self.narrow(node['left'], [c for c in need if c in left]),
                    'right': self.narrow(node['right'], [c for c in need if c in right])}
        if op in SET_OPS:                       # positions matter on both sides: keep everything
            return {**node, 'left': self.walk(node['left'], self.cols(node['left'])),
                    'right': self.walk(node['right'], self.cols(node['right']))}
        child = node['child']
        below = self.cols(child)
        if op == 'select':
            return {**node, 'child': self.walk(child, in_order(required, self.needs(below, node['condition'])))}
        if op == 'project':
            sources = in_order(resolve(below, c[0] if isinstance(c, list) else c) for c in node['columns'])
            return {**node, 'child': self.walk(child, sources)}
        if op == 'rename':
            mine = self.cols(node)
            return {**node, 'child': self.walk(child, [below[mine.index(c)] for c in required if c in mine])}
        if op == 'group_by':
            need = in_order((resolve(below, k) for k in node['keys']),
                            (resolve(below, a[1]) for a in node['aggregates'] if a[1] != '*'))
            return {**node, 'child': self.narrow(child, need)}
        if op == 'sort':
            need = in_order(required, (resolve(below, k) for k, _ in node['keys']))
            return {**node, 'child': self.narrow(child, need)}
        if op == 'distinct':                    # which rows are duplicates depends on every column
            return {**node, 'child': self.walk(child, below)}
        return {**node, 'child': self.walk(child, required)}            # limit


# ------------------------------------------------------------------ rule 7

def tidy(node, catalog):
    node = dict(node)
    for field in ('child', 'left', 'right'):
        if field in node:
            node[field] = tidy(node[field], catalog)
    if node['op'] == 'project' and node['columns'] == output_columns(node['child'], catalog):
        return node['child']                                    # it drops nothing
    if node['op'] == 'select' and node['child']['op'] == 'select':
        both = Logic('and', left=parse_condition(node['condition']),
                     right=parse_condition(node['child']['condition']))
        return {'op': 'select', 'condition': condition_text(both), 'child': node['child']['child']}
    return node


def rewrite(plan, catalog, disabled=()):
    disabled = set(disabled)
    plan = Sinker(catalog, disabled).walk(plan)
    if 6 not in disabled:
        plan = Projector(catalog).walk(plan, output_columns(plan, catalog))
    if 7 not in disabled:
        plan = tidy(plan, catalog)
    return plan


def tree_text(node, indent=''):
    """A logical plan as an indented tree, a rename over a scan on one line."""
    op = node['op']
    if op == 'scan':
        return f"{indent}scan {node['table']}"
    if op == 'rename' and node['child']['op'] == 'scan':
        return f"{indent}rename {node['to']} (scan {node['child']['table']})"
    if op in ('select', 'join'):
        head = f"{op} {node['condition']}"
    elif op == 'project':
        head = 'project [' + ', '.join(f'{c[0]} as {c[1]}' if isinstance(c, list) else c for c in node['columns']) + ']'
    elif op == 'rename':
        head = f"rename {node['to']}"
    elif op == 'group_by':
        head = (f"group_by [{', '.join(node['keys'])}] "
                + ', '.join(f'{f}({a}) as {n}' for f, a, n in node['aggregates']))
    elif op == 'sort':
        head = 'sort [' + ', '.join(f'{k} {d}' for k, d in node['keys']) + ']'
    elif op == 'limit':
        head = f"limit {node['count']}"
    else:
        head = op
    lines = [indent + head]
    for field in ('child', 'left', 'right'):
        if field in node:
            lines.append(tree_text(node[field], indent + '  '))
    return '\n'.join(lines)
