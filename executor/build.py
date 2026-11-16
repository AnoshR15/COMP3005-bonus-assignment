"""From a physical plan in JSON to a tree of operators.

Two steps, both before any row is read:

    validate(plan)      the JSON has the right shape: known operators, required fields, field
                        types. Nothing is looked up. An error names the node and where it is,
                        for example   Node 'sort' at $.child: missing required field 'keys'
    build(plan, ctx)    creates the operators bottom up. Tables are looked up in the catalog,
                        every column name and condition is resolved against the schema of the
                        input it applies to, and types are checked.

The physical operators and their fields:

    seqscan           table
    filter            condition, child
    project           columns, child                 a column is "R.a" or ["R.a", "new name"]
    rename            to, child                      gives every column the qualifier `to`
    distinct          mode, child                    mode is "sort" or "hash"
    sort              keys, child                    keys is [["R.a", "asc"], ["R.b", "desc"]]
    group_by          mode, keys, aggregates, child  an aggregate is ["count", "*"] or
                                                     ["avg", "R.x"], optionally with a third
                                                     element, the output name
    limit             count, child                   `n` is accepted in place of `count`
    nested_loop_join  condition, left, right         without `condition` it is a cross product
    sort_merge_join   condition, left, right
    hash_join         condition, left, right         left is the build side
    union_all, union, intersect, except              left, right
"""
from .context import PlanError
from .groupby import GroupBy, Distinct, AGGREGATES
from .joins import NestedLoopJoin, SortMergeJoin, HashJoin
from .operators import SeqScan, Filter, Project, Rename, Limit, UnionAll, column_index
from .setops import Union, Intersect, Except
from .sort import Sort

UNARY = {'child': dict}
BINARY = {'left': dict, 'right': dict}
JOIN = {'condition': str, **BINARY}
FIELDS = {
    'seqscan': {'table': str},
    'filter': {'condition': str, **UNARY},
    'project': {'columns': list, **UNARY},
    'rename': {'to': str, **UNARY},
    'distinct': {'mode': str, **UNARY},
    'sort': {'keys': list, **UNARY},
    'group_by': {'mode': str, 'keys': list, 'aggregates': list, **UNARY},
    'limit': {'count': int, **UNARY},
    'nested_loop_join': JOIN, 'sort_merge_join': JOIN, 'hash_join': JOIN,
    'union_all': BINARY, 'union': BINARY, 'intersect': BINARY, 'except': BINARY,
}
OPTIONAL = {'nested_loop_join': {'condition'}}      # no condition: every pair, a cross product
TYPE_NAMES = {str: 'a string', dict: 'an object', list: 'a list', int: 'an integer'}
ALIASES = {'limit': {'n': 'count'}}         # other spellings of a field, until the schema settles


def validate(node, path='$'):
    if not isinstance(node, dict):
        raise PlanError(f'Node at {path}: a plan node must be an object')
    op = node.get('op')
    if not isinstance(op, str):
        raise PlanError(f"Node at {path}: missing required field 'op'")
    where = f"Node '{op}' at {path}"
    if op not in FIELDS:
        raise PlanError(f"{where}: unknown operator '{op}'")
    for other, field in ALIASES.get(op, {}).items():
        if other in node and field not in node:
            node[field] = node.pop(other)
    for field, typ in FIELDS[op].items():
        if field not in node:
            if field in OPTIONAL.get(op, ()):
                continue
            raise PlanError(f"{where}: missing required field '{field}'")
        if not isinstance(node[field], typ) or isinstance(node[field], bool):
            raise PlanError(f"{where}: field '{field}' must be {TYPE_NAMES[typ]}")
        if typ is str and not node[field].strip():
            raise PlanError(f"{where}: field '{field}' cannot be empty")
    for field in node:
        if field != 'op' and field not in FIELDS[op]:
            raise PlanError(f"{where}: unexpected field '{field}'")

    if 'mode' in node and node['mode'] not in ('sort', 'hash'):
        raise PlanError(f"{where}: field 'mode' must be \"sort\" or \"hash\", not \"{node['mode']}\"")
    if op == 'project':
        if not node['columns']:
            raise PlanError(f"{where}: field 'columns' cannot be empty")
        for c in node['columns']:
            renamed = isinstance(c, list) and len(c) == 2 and all(isinstance(x, str) for x in c)
            if not isinstance(c, str) and not renamed:
                raise PlanError(f"{where}: field 'columns': every entry must be a column name "
                                'or [column name, new name]')
    if op == 'sort':
        if not node['keys']:
            raise PlanError(f"{where}: field 'keys' cannot be empty")
        for k in node['keys']:
            if not (isinstance(k, list) and len(k) == 2 and isinstance(k[0], str) and k[1] in ('asc', 'desc')):
                raise PlanError(f"{where}: field 'keys': every key must be [column name, \"asc\" or \"desc\"]")
    if op == 'group_by':
        if not all(isinstance(k, str) for k in node['keys']):
            raise PlanError(f"{where}: field 'keys' must be a list of column names")
        for a in node['aggregates']:
            if not (isinstance(a, list) and len(a) in (2, 3) and all(isinstance(x, str) for x in a)):
                raise PlanError(f"{where}: field 'aggregates': every entry must be [function, column or \"*\"]")
            if a[0] not in AGGREGATES:
                raise PlanError(f"{where}: field 'aggregates': unknown aggregate '{a[0]}'; "
                                f"expected one of {', '.join(AGGREGATES)}")
        if not node['keys'] and not node['aggregates']:
            raise PlanError(f"{where}: 'keys' and 'aggregates' cannot both be empty")
    if op == 'limit' and node['count'] < 0:
        raise PlanError(f"{where}: field 'count' cannot be negative")
    for field in ('child', 'left', 'right'):
        if field in node:
            validate(node[field], f'{path}.{field}')
    return op


def build(node, ctx, path='$'):
    """A validated plan node -> its operator, with its inputs built first."""
    op = node['op']
    where = f"Node '{op}' at {path}"
    if op == 'seqscan':
        return SeqScan(ctx, node['table'], where)
    if 'child' in node:
        child = build(node['child'], ctx, path + '.child')
        if op == 'filter':
            return Filter(ctx, child, node['condition'], where)
        if op == 'project':
            return Project(ctx, child, node['columns'], where)
        if op == 'rename':
            return Rename(ctx, child, node['to'], where)
        if op == 'limit':
            return Limit(ctx, child, node['count'], where)
        if op == 'distinct':
            return Distinct(ctx, child, node['mode'], where)
        if op == 'group_by':
            return GroupBy(ctx, child, node['mode'], node['keys'], node['aggregates'], where)
        keys = [(column_index(child.columns, name, where), direction == 'desc') for name, direction in node['keys']]
        return Sort(ctx, child, keys, where)
    left = build(node['left'], ctx, path + '.left')
    right = build(node['right'], ctx, path + '.right')
    if op == 'nested_loop_join':
        return NestedLoopJoin(ctx, left, right, node.get('condition'), where)
    if op == 'sort_merge_join':
        return SortMergeJoin(ctx, left, right, node['condition'], where)
    if op == 'hash_join':
        return HashJoin(ctx, left, right, node['condition'], where)
    return {'union_all': UnionAll, 'union': Union, 'intersect': Intersect, 'except': Except}[op](ctx, left, right, where)
