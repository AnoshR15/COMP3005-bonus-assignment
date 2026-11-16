"""From a logical plan to a physical plan: rewrite, estimate, enumerate join orders, choose.

    result = Planner(catalog, frames).optimize(logical_plan)
    result.plan.to_json()     the physical plan, valid input for the Module 2 executor
    result.explain()          one line per node with est_rows and est_cost
    result.dp_text()          the dynamic programming table of every join block

ADDING UP COSTS

The formulas of cost.py each include reading their inputs once. A plan's cost is built from
them like this, for an operator with formula f over an input X of P pages:

    X is a table access (a scan, possibly under filters, projections and renames)
        cost = cost(X) - P + f        f has paid for reading P pages, the size of X's output.
                                      Reading the table really cost cost(X), its full size.
                                      For a plain scan the two are equal and nothing is added.
    X is anything else (a join, a sort, a subquery)
        cost = cost(X) + f            producing X is one cost; f then charges P again for
                                      reading it. That second charge is the specification's
                                      convention (its example adds 110 + 20 + 400), and it is
                                      I/O the executor does not do when X is pipelined into f.

est_cost in explain is, by default, the formula f of that one operator (Result.explain says what
the other two choices are). A node's cost here is always the whole subtree.

JOIN ENUMERATION

A join block is a maximal tree of join and cross nodes. Its leaves are whatever hangs below
(each planned on its own), its predicates are the conjuncts of the join conditions. Dynamic
programming over subsets of the leaves, left-deep only:

    best[{r}]  = the plan of leaf r
    best[S]    = the cheapest, over every r in S and every join algorithm, of
                 best[S - {r}] joined with r          (r is the right input, always a leaf)

A pairing with no predicate between S - {r} and r is a cross product and is skipped; only if
the whole block cannot be joined without one is it allowed. The size of best[S] depends on S
alone, not on the order that produced it: the product of the leaves' rows and the selectivities
of every predicate inside S.

For each pairing: nested_loop_join with all the predicates that become applicable; if there is
an equality between the two sides, also hash_join and sort_merge_join on those equalities, with
any other predicate as a filter above the join.
"""
import math
from source.ast import Attr, Compare, Logic
from source.condition import parse_condition, condition_text
from store.record import INT, VARCHAR, REAL
from . import cost as C
from .estimate import (Est, ColEst, conjuncts, filtered, selectivity, two_columns, round_rows, pages_for,
                       DEFAULT_OTHER)
from .rewrite import rewrite, resolve, attributes, tree_text, output_columns, RewriteError, SET_OPS

JOINS = ('nested_loop_join', 'hash_join', 'sort_merge_join')
MAX_DP_LEAVES = 10          # beyond this, 2^n subsets is too many; joins stay in written order


class PlanningError(Exception):
    pass


class PNode:
    """A node of the physical plan with the optimizer's estimates."""

    def __init__(self, op, fields, children, est, cost, pages, access=False, bare=False, formula=0):
        self.op = op
        self.fields = fields            # the plan's own fields: table, condition, columns, ...
        self.children = children        # [(role, PNode)], role is child, left or right
        self.est = est
        self.rows = est.rows
        self.cost = cost                # estimated I/O of the whole subtree
        self.formula = formula          # this operator's formula of cost.py, applied to its inputs' pages
        self.pages = pages              # estimated pages of the output
        self.access = access            # a table scan under nothing but filters, projections, renames
        self.bare = bare                # a table scan under nothing but renames

    def to_json(self):
        plan = {'op': self.op, **self.fields}
        for role, child in self.children:
            plan[role] = child.to_json()
        return plan

    def own_cost(self):
        return self.cost - sum(child.cost for _, child in self.children)

    def lead(self):
        """What must be added for this node as an input of a formula that reads it (see top)."""
        return self.cost - self.pages if self.access else self.cost

    def label(self):
        f = self.fields
        if self.op == 'seqscan':
            return f"seqscan {f['table']}"
        if self.op == 'rename':
            return f"rename {f['to']}"
        if self.op == 'limit':
            return f"limit {f['count']}"
        if self.op == 'project':
            return 'project (' + ', '.join(f'{c[0]} as {c[1]}' if isinstance(c, list) else c for c in f['columns']) + ')'
        if self.op == 'sort':
            return 'sort (' + ', '.join(f'{k} {d}' for k, d in f['keys']) + ')'
        if self.op in ('group_by', 'distinct'):
            what = ''
            if self.op == 'group_by':
                what = ' (' + ', '.join(f['keys'] + [f'{a[0]}({a[1]})' for a in f['aggregates']]) + ')'
            return f"{self.op} {f['mode']}{what}"
        if 'condition' in f:
            return f"{self.op} ({f['condition']})"
        return self.op + (' (cross product)' if self.op == 'nested_loop_join' else '')

    def walk(self, depth=0):
        """(depth, node) in plan order: a node, then its inputs left to right."""
        yield depth, self
        for _, child in self.children:
            yield from child.walk(depth + 1)


class Result:

    def __init__(self, plan, before, after, tables):
        self.plan = plan
        self.before = before            # the logical plan as given
        self.after = after              # the logical plan after the rewrite rules
        self.tables = tables            # one dynamic programming table per join block

    def explain(self, subtree=False, own=False):
        """One line per node: est_rows and est_cost. What est_cost is:

            default   the operator's formula applied to the estimated pages of its inputs: M for a
                      scan, 0 for a filter or a projection, 3 (M + N) for a hash join. This is the
                      form of the specification's example, and each line can be checked by hand.
                      The formulas of the joins and the sort include reading their inputs, so
                      these lines do not add up to the cost of the plan.
            subtree   the estimated cost of everything from this node down: the number the join
                      enumeration compares, and the top line is the cost of the plan
            own       a node's share of the plan's cost, its subtree minus its inputs' subtrees;
                      these lines do add up to the cost of the plan"""
        nodes = list(self.plan.walk())
        labels = ['  ' * depth + node.label() for depth, node in nodes]
        width = max(len(label) for label in labels) + 2

        def cost(node):
            return node.cost if subtree else node.own_cost() if own else node.formula
        return '\n'.join(f"{label:<{width}} est_rows={node.rows:<9} est_cost={round(cost(node))}"
                         for label, (_, node) in zip(labels, nodes))

    def rewrite_text(self):
        return 'before\n' + tree_text(self.before, '  ') + '\n\nafter\n' + tree_text(self.after, '  ')

    def dp_text(self, alternatives=False):
        if not self.tables:
            return 'no join in this plan'
        out = []
        for table in self.tables:
            rows = [('subset', 'best plan considered', 'rows', 'pages', 'cost')]
            for entry in table:
                rows.append((entry['subset'], entry['plan'], entry['rows'], entry['pages'], entry['cost']))
                shown = entry['alternatives'] if alternatives else entry['alternatives'][:1] if entry['last'] else []
                for text, value in shown:
                    rows.append(('', 'alternative: ' + text, '', '', value))
            widths = [max(len(str(r[i])) for r in rows) for i in range(5)]
            out.append('\n'.join('  '.join(str(v).ljust(w) if i < 2 else str(v).rjust(w)
                                           for i, (v, w) in enumerate(zip(r, widths))).rstrip() for r in rows))
        return '\n\n'.join(out)


def and_all(trees):
    result = trees[0]
    for tree in trees[1:]:
        result = Logic('and', left=result, right=tree)
    return result


class Planner:

    def __init__(self, catalog, frames, disabled=(), assume=None):
        """disabled: rewrite rule numbers to leave out. assume: {"rows": {...}, "pages": {...}}
        keyed by a leaf name or by several joined with commas in alphabetical order ("d,e");
        those estimates are used in place of the computed ones."""
        if frames < 3:
            raise PlanningError('planning needs --frames of at least 3')
        self.catalog = catalog
        self.frames = frames
        self.disabled = set(disabled)
        self.assume = assume or {}
        self.tables = []

    def optimize(self, logical):
        try:
            after = rewrite(logical, self.catalog, self.disabled)
            self.tables = []
            return Result(self.plan(after, None, True), logical, after, self.tables)
        except RewriteError as e:
            raise PlanningError(str(e)) from None

    # ---------------------------------------------------------------- one node at a time

    def plan(self, node, needed, ordered=False):
        """The physical plan of a logical node.

        needed   the set of its output columns that something above uses, or None for all
        ordered  whether whoever consumes the output depends on the order of its columns (the
                 final result does, and so do the set operators). A join block may come out
                 with its inputs in another order than written; where that matters it puts
                 the columns back with a projection."""
        op = node['op']
        if op == 'scan':
            return self.scan(node)
        if op in ('join', 'cross'):
            return self.join_block(node, needed, ordered)
        if op in SET_OPS:
            return self.set_op(node)
        columns = output_columns(node['child'], self.catalog)
        if op == 'rename':
            mine = output_columns(node, self.catalog)
            below = None if needed is None else {columns[mine.index(c)] for c in needed if c in mine}
            child = self.plan(node['child'], below, ordered)
            est = Est(child.rows, {f"{node['to']}.{name.rpartition('.')[2]}": col
                                   for name, col in child.est.cols.items()})
            return PNode('rename', {'to': node['to']}, [('child', child)], est, child.cost, child.pages,
                         child.access, child.bare)
        if op == 'select':
            refs = {resolve(columns, r) for r in attributes(parse_condition(node['condition']))} - {None}
            child = self.plan(node['child'], None if needed is None else set(needed) | refs, ordered)
            est = filtered(child.est, parse_condition(node['condition']))
            pages = min(child.pages, pages_for(est.rows, est.width()))
            return PNode('filter', {'condition': node['condition']}, [('child', child)], est, child.cost,
                         pages, child.access)
        if op == 'project':
            sources = [resolve(columns, c[0] if isinstance(c, list) else c) for c in node['columns']]
            child = self.plan(node['child'], set(sources) - {None})
            cols = {}
            for entry, source in zip(node['columns'], sources):
                if source is None:
                    raise PlanningError(f"project: unknown column '{entry}'")
                cols[entry[1] if isinstance(entry, list) else source] = child.est.cols[source]
            est = Est(child.rows, cols)
            pages = min(child.pages, pages_for(est.rows, est.width()))
            return PNode('project', {'columns': node['columns']}, [('child', child)], est, child.cost,
                         pages, child.access)
        if op == 'limit':
            child = self.plan(node['child'], needed, ordered)
            est = child.est.with_rows(max(1, min(child.rows, node['count'])))
            pages = max(1, math.ceil(child.pages * est.rows / max(child.rows, 1)))
            return PNode('limit', {'count': node['count']}, [('child', child)], est, child.cost, pages)
        if op == 'sort':
            keys = {resolve(columns, k) for k, _ in node['keys']} - {None}
            child = self.plan(node['child'], None if needed is None else set(needed) | keys, ordered)
            formula = C.sort_cost(child.pages, self.frames)
            return PNode('sort', {'keys': node['keys']}, [('child', child)], child.est, child.lead() + formula,
                         child.pages, formula=formula)
        if op == 'distinct':
            child = self.plan(node['child'], None, ordered)
            rows = self.groups(child.est, list(child.est.cols))
            return self.grouped('distinct', {}, child, child.est.with_rows(rows), child.est.width())
        if op == 'group_by':
            keys = [resolve(columns, k) for k in node['keys']]
            args = [resolve(columns, a[1]) for a in node['aggregates'] if a[1] != '*']
            child = self.plan(node['child'], set(keys + args) - {None})
            rows = self.groups(child.est, keys) if keys else 1
            cols = {k: child.est.cols[k] for k in keys}
            for func, arg, name in node['aggregates']:
                source = child.est.cols[resolve(columns, arg)] if arg != '*' else None
                if func in ('min', 'max'):
                    cols[name] = ColEst(source.kind, source.width, rows)
                else:
                    cols[name] = ColEst(REAL if func == 'avg' else INT, 8 if func == 'avg' else 4, rows)
            fields = {'keys': node['keys'], 'aggregates': node['aggregates']}
            key_width = sum(child.est.cols[k].width for k in keys) + 8 * len(node['aggregates'])
            return self.grouped('group_by', fields, child, Est(rows, cols), key_width)
        raise PlanningError(f"unknown logical operator '{op}'")

    def scan(self, node):
        info = self.catalog.get_table(node['table'])
        if info is None:
            raise PlanningError(f"unknown table '{node['table']}'")
        stats = (info.stats or {}).get('columns', {})
        cols = {f"{info.name}.{name}": ColEst.from_stats(kind, size, stats.get(name))
                for name, kind, size in info.columns}
        est = Est(max(1, info.num_tuples), cols)
        return PNode('seqscan', {'table': info.name}, [], est, info.num_pages, info.num_pages, True, True,
                     formula=info.num_pages)

    def groups(self, est, keys):
        """Estimated number of distinct combinations of the key columns: the product of their
        distinct values, never more than the rows. An unknown count is taken as a tenth of the rows."""
        product = 1
        for k in keys:
            ndv = est.ndv(k)
            product *= ndv if ndv is not None else max(1, est.rows // 10)
            if product >= est.rows:
                return est.rows
        return max(1, product)

    def grouped(self, op, fields, child, est, group_width):
        """group_by or distinct: the cheaper of sort mode and hash mode (hash on a tie)."""
        if op == 'group_by' and not fields['keys']:
            mode, formula = 'hash', child.pages                 # one group: nothing to sort or hash
        else:
            by_sort = C.sort_cost(child.pages, self.frames)
            by_hash = C.hash_group_cost(child.pages, pages_for(est.rows, group_width), self.frames)
            mode, formula = ('sort', by_sort) if by_sort < by_hash else ('hash', by_hash)
        pages = pages_for(est.rows, est.width())
        return PNode(op, {'mode': mode, **fields}, [('child', child)], est, child.lead() + formula, pages,
                     formula=formula)

    def set_op(self, node):
        left, right = self.plan(node['left'], None, True), self.plan(node['right'], None, True)
        op, b = node['op'], self.frames
        cols = dict(zip(left.est.cols, [c.copy() for c in left.est.cols.values()]))
        for mine, other in zip(cols.values(), right.est.cols.values()):
            mine.hist = None
            if mine.ndv is not None and other.ndv is not None:
                mine.ndv = mine.ndv + other.ndv if op in ('union', 'union_all') else min(mine.ndv, other.ndv)
        both = Est(left.rows + right.rows, cols)
        if op == 'union_all':
            return PNode(op, {}, [('left', left), ('right', right)], both, left.cost + right.cost,
                         left.pages + right.pages)
        if op == 'union':
            est = both.with_rows(self.groups(both, list(cols)))
            formula = C.sort_cost(left.pages + right.pages, b)
        else:
            rows = min(left.rows, right.rows) if op == 'intersect' else left.rows
            est = Est(rows, cols)
            est = est.with_rows(self.groups(est, list(cols)))
            formula = C.sort_cost(left.pages, b) + C.sort_cost(right.pages, b)
        return PNode(op, {}, [('left', left), ('right', right)], est, left.lead() + right.lead() + formula,
                     pages_for(est.rows, est.width()), formula=formula)

    # ---------------------------------------------------------------- join blocks

    def join_block(self, node, needed, ordered):
        leaves, conditions = [], []

        def collect(n):
            if n['op'] in ('join', 'cross'):
                collect(n['left'])
                collect(n['right'])
                if n['op'] == 'join':
                    conditions.extend(conjuncts(parse_condition(n['condition'])))
            else:
                leaves.append(n)
        collect(node)

        columns = output_columns(node, self.catalog)
        plans = [self.plan(leaf, None) for leaf in leaves]
        block = Block(self, plans, [leaf_name(leaf, i) for i, leaf in enumerate(leaves)], conditions,
                      set(columns) if needed is None else set(needed))
        root = block.solve()
        if ordered and list(root.est.cols) != columns:      # the joins were reordered: restore the columns
            est = Est(root.rows, {c: root.est.cols[c] for c in columns})
            root = PNode('project', {'columns': columns}, [('child', root)], est, root.cost, root.pages)
        return root


def leaf_name(node, index):
    """What to call a leaf of a join block: its alias, or its table."""
    while True:
        if node['op'] == 'rename':
            return node['to']
        if node['op'] == 'scan':
            return node['table']
        if 'child' not in node:
            return f'#{index + 1}'
        node = node['child']


class Predicate:

    def __init__(self, tree, leaves):
        self.tree = tree
        self.text = condition_text(tree)
        self.where = {}                                 # attribute -> index of the leaf that has it
        for attr in attributes(tree):
            hits = [i for i, leaf in enumerate(leaves) if leaf.est.find(attr) is not None]
            if len(hits) != 1:
                raise PlanningError(f"join condition '{self.text}': column "
                                    f"'{attr.relation + '.' if attr.relation else ''}{attr.name}' "
                                    + ('is not in any input' if not hits else 'is in more than one input'))
            self.where[attr] = hits[0]
        self.leaves = frozenset(self.where.values())
        self.mask = sum(1 << i for i in self.leaves)
        t = tree
        self.equality = (isinstance(t, Compare) and t.op == '=' and isinstance(t.left, Attr)
                         and isinstance(t.right, Attr) and self.where[t.left] != self.where[t.right])
        # a = b and b = a are one predicate: order and write them the same way
        self.key = ' = '.join(sorted(self.text.split(' = '))) if self.equality else self.text
        if self.equality:
            self.selectivity = two_columns(leaves[self.where[t.left]].est, leaves[self.where[t.right]].est, t)
        elif len(self.leaves) >= 2:
            self.selectivity = DEFAULT_OTHER
        else:
            self.selectivity = selectivity(leaves[next(iter(self.leaves))].est if self.leaves else leaves[0].est, tree)

    def oriented(self, outer_mask):
        """An equality written  outer column = inner column; anything else as it is."""
        t = self.tree
        if not self.equality or (1 << self.where[t.left]) & outer_mask:
            return t
        return Compare(t.right, '=', t.left)


class Block:
    """One join block: the dynamic programming of the module docstring."""

    def __init__(self, planner, leaves, names, conditions, needed):
        self.planner = planner
        self.frames = planner.frames
        self.leaves = leaves
        self.names = names
        self.n = len(leaves)
        self.full = (1 << self.n) - 1
        self.needed = needed
        self.project = 6 not in planner.disabled
        self.predicates = sorted((Predicate(tree, leaves) for tree in conditions), key=lambda p: p.key)
        assume = planner.assume
        for i, name in enumerate(names):                # estimates the caller insists on, for leaves
            leaf = leaves[i]
            if name in assume.get('rows', {}):
                leaf.rows = assume['rows'][name]
                leaf.est = leaf.est.with_rows(leaf.rows)
                leaf.pages = min(leaf.pages, pages_for(leaf.rows, leaf.est.width())) if not leaf.bare else leaf.pages
            if name in assume.get('pages', {}):
                leaf.cost = leaf.cost - leaf.pages + assume['pages'][name] if leaf.bare else leaf.cost
                leaf.pages = assume['pages'][name]
        self.size = {}                                  # mask -> (rows, pages, columns kept)
        self.best = {}                                  # mask -> dict describing the cheapest plan

    def key(self, mask):
        return ','.join(sorted(self.names[i] for i in range(self.n) if mask >> i & 1))

    def show(self, mask):
        return '{' + ','.join(self.names[i] for i in range(self.n) if mask >> i & 1) + '}'

    def inside(self, mask):
        """The predicates that involve only leaves of `mask`. For a single leaf: none yet; a
        predicate on one leaf is applied at the first join that leaf takes part in."""
        if mask & (mask - 1) == 0:
            return []
        return [p for p in self.predicates if p.mask & ~mask == 0]

    def measure(self, mask):
        """(rows, pages, kept columns) of the join of the leaves in `mask`: order does not matter."""
        if mask in self.size:
            return self.size[mask]
        members = [self.leaves[i] for i in range(self.n) if mask >> i & 1]
        if len(members) == 1:
            result = (members[0].rows, members[0].pages, list(members[0].est.cols))
        else:
            assume = self.planner.assume
            rows = 1.0
            for leaf in members:
                rows *= leaf.rows
            for p in self.inside(mask):
                rows *= p.selectivity
            rows = assume.get('rows', {}).get(self.key(mask), round_rows(rows))
            columns = [c for leaf in members for c in leaf.est.cols]
            kept = columns
            if self.project and mask != self.full:      # rule 6: carry on only what is still needed
                later = set()
                for p in self.predicates:
                    if p.mask & ~mask:
                        later |= {leaf.est.find(a) for leaf in members for a in p.where} - {None}
                kept = [c for c in columns if c in self.needed or c in later] or columns[:1]
            keeping = set(kept)
            width = sum(col.width for leaf in members for c, col in leaf.est.cols.items() if c in keeping)
            pages = assume.get('pages', {}).get(self.key(mask), pages_for(rows, width))
            result = (rows, pages, kept)
        self.size[mask] = result
        return result

    # ---------------------------------------------------------------- the table

    def candidates(self, mask, allow_cross):
        """Every way to make `mask` from a smaller best plan and one more leaf."""
        found = []
        rows, pages, _ = self.measure(mask)
        for r in range(self.n):
            bit = 1 << r
            rest = mask & ~bit
            if not mask & bit or rest not in self.best:
                continue
            new = [p for p in self.inside(mask) if p not in self.inside(rest)]
            joining = [p for p in new if p.mask & bit and p.mask & rest]
            if not joining and not allow_cross:
                continue
            outer, inner = self.best[rest], self.leaves[r]
            m, n = self.measure(rest)[1], inner.pages
            lead = outer['cost'] if outer['join'] else self.leaves[outer['leaf']].lead()
            keys = [p for p in joining if p.equality]
            def who(algorithm):
                return f'{self.show(rest)} outer, {algorithm} with {self.names[r]} inner: '
            prefix = f'{round(lead)} + ' if lead else ''

            # Each candidate ends with the join's own formula: what explain prints on its line.
            blocks = C.nested_loop_blocks(m, self.frames)
            if inner.bare:
                formula, text = m + blocks * n, f'{prefix}{m} + {blocks} x {n}'
                total = lead + formula
            else:                                       # the executor spools such an inner input once
                formula = m + n + blocks * n            # the spool is written once and read per block
                total = lead + inner.cost + formula
                text = f'{prefix}{m} + {round(inner.cost)} + {n} + {blocks} x {n}'
            found.append((total, 'nested_loop_join', rest, r, new, [], who('nested loop') + text, formula))
            if keys:
                rest_preds = [p for p in new if p not in keys]
                both = lead + inner.lead()
                prefix = f'{round(both)} + ' if both else ''
                levels = C.hash_levels(m, self.frames)
                text = f'{prefix}3 x ({m} + {n})' + (f' + {2 * (levels - 1)} x ({m} + {n})' if levels > 1 else '')
                formula = C.hash_cost(m, n, self.frames)
                found.append((both + formula, 'hash_join', rest, r, keys, rest_preds, who('hash join') + text,
                              formula))
                text = (f'{prefix}sort({m}) + sort({n}) + 2 x ({m} + {n}) = {prefix}'
                        f'{C.sort_cost(m, self.frames)} + {C.sort_cost(n, self.frames)} + {2 * (m + n)}')
                formula = C.sort_merge_cost(m, n, self.frames)
                found.append((both + formula, 'sort_merge_join', rest, r, keys, rest_preds,
                              who('sort-merge') + text, formula))
        return found

    def fill(self, allow_cross):
        for mask in sorted(range(1, self.full + 1), key=lambda m: (bin(m).count('1'), m)):
            if mask in self.best or mask & (mask - 1) == 0:
                continue
            found = self.candidates(mask, allow_cross)
            if not found:
                continue
            found.sort(key=lambda c: c[0])              # stable: ties keep the order they were tried in
            total, algorithm, rest, r, condition, residual, text, formula = found[0]
            self.best[mask] = {'cost': total, 'join': True, 'algorithm': algorithm, 'rest': rest, 'leaf': r,
                               'condition': condition, 'residual': residual, 'text': text, 'cross': allow_cross,
                               'formula': formula, 'alternatives': [(c[6], round(c[0])) for c in found[1:]]}

    def solve(self):
        for i, leaf in enumerate(self.leaves):
            self.best[1 << i] = {'cost': leaf.cost, 'join': False, 'leaf': i}
        if self.n > MAX_DP_LEAVES:                      # too many subsets: keep the written order
            for i in range(1, self.n):
                mask = (1 << (i + 1)) - 1
                found = [c for c in self.candidates(mask, True) if c[3] == i]
                total, algorithm, rest, r, condition, residual, text, formula = min(found, key=lambda c: c[0])
                self.best[mask] = {'cost': total, 'join': True, 'algorithm': algorithm, 'rest': rest, 'leaf': r,
                                   'condition': condition, 'residual': residual, 'text': text, 'cross': False,
                                   'formula': formula, 'alternatives': []}
        else:
            self.fill(allow_cross=False)
            if self.full not in self.best:              # the block cannot be joined without a cross product
                self.fill(allow_cross=True)
        self.planner.tables.append(self.table())
        return self.build(self.full)

    def table(self):
        entries = []
        for mask in sorted(range(1, self.full + 1), key=lambda m: (bin(m).count('1'), m)):
            best = self.best.get(mask)
            entry = {'subset': self.show(mask), 'rows': '', 'pages': '', 'cost': '', 'alternatives': [],
                     'last': mask == self.full}
            if best is None:
                entry['plan'] = 'no join predicate, skipped'
            else:
                rows, pages, _ = self.measure(mask)
                entry.update(rows=rows, pages=pages, cost=round(best['cost']))
                if best['join']:
                    entry['plan'] = best['text'] + (' (cross product)' if best['cross'] and not best['condition'] else '')
                    entry['alternatives'] = best['alternatives']
                else:
                    leaf = self.leaves[best['leaf']]
                    entry['plan'] = ('scan ' if leaf.bare else 'access ') + self.names[best['leaf']]
            entries.append(entry)
        return entries

    # ---------------------------------------------------------------- the chosen plan

    def build(self, mask):
        best = self.best[mask]
        if not best['join']:
            return self.leaves[best['leaf']]
        outer, inner = self.build(best['rest']), self.leaves[best['leaf']]
        rows, pages, kept = self.measure(mask)
        cols = {**outer.est.cols, **inner.est.cols}
        fields = {}
        if best['condition']:                           # none: a cross product
            fields['condition'] = condition_text(and_all([p.oriented(best['rest']) for p in best['condition']]))
        width = sum(c.width for c in cols.values())
        joined = rows
        for p in best['residual']:                      # the join itself has not applied these yet
            joined = joined / p.selectivity if p.selectivity else joined
        joined = round_rows(joined)
        narrower = mask != self.full and len(kept) < len(cols)
        exact = not best['residual'] and not narrower   # then this node's output is the subset's
        node = PNode(best['algorithm'], fields, [('left', outer), ('right', inner)], Est(joined, cols),
                     best['cost'], pages if exact else pages_for(joined, width), formula=best['formula'])
        if best['residual']:
            text = condition_text(and_all([p.tree for p in best['residual']]))
            node = PNode('filter', {'condition': text}, [('child', node)], Est(rows, cols), best['cost'],
                         pages_for(rows, width) if narrower else pages)
        if narrower:
            node = PNode('project', {'columns': kept}, [('child', node)],
                         Est(rows, {c: cols[c] for c in kept}), best['cost'], pages)
        return node


# ---------------------------------------------------------------------- no optimizer at all

def naive(logical):
    """The plan with no optimization: every operator where the query put it, joins in written
    order, block nested loop for every join, sorting for every group_by and distinct."""
    op = logical['op']
    inputs = {f: naive(logical[f]) for f in ('child', 'left', 'right') if f in logical}
    if op == 'scan':
        return {'op': 'seqscan', 'table': logical['table']}
    if op == 'select':
        return {'op': 'filter', 'condition': logical['condition'], **inputs}
    if op == 'join':
        return {'op': 'nested_loop_join', 'condition': logical['condition'], **inputs}
    if op == 'cross':
        return {'op': 'nested_loop_join', **inputs}
    if op == 'distinct':
        return {'op': 'distinct', 'mode': 'sort', **inputs}
    if op == 'group_by':
        return {'op': 'group_by', 'mode': 'sort', 'keys': logical['keys'], 'aggregates': logical['aggregates'],
                **inputs}
    return {**{k: v for k, v in logical.items() if k not in inputs}, **inputs}
