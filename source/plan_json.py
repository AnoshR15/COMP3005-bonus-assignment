"""The logical plan as JSON, the format the modules of the database system exchange.

    to_json(node)   -> dict          from_json(dict) -> node
    dumps(node)     -> JSON text     loads(text)     -> node

Every node is an object with an "op" field. The operator and field names are the ones fixed by
the Module 3 specification. Conditions travel as strings and are read back with the condition
parser; column references are strings such as "R.a".

    {"op": "scan",      "table": "R"}
    {"op": "select",    "condition": "R.a > 500", "child": {...}}
    {"op": "project",   "columns": ["R.a", ["S.c", "c2"]], "child": {...}}     ["S.c", "c2"] is S.c AS c2
    {"op": "rename",    "to": "E2", "child": {...}}
    {"op": "distinct",  "child": {...}}
    {"op": "group_by",  "keys": ["R.a"], "aggregates": [["count", "*", "n"]], "child": {...}}
    {"op": "sort",      "keys": [["n", "desc"]], "child": {...}}
    {"op": "limit",     "count": 10, "child": {...}}
    {"op": "join",      "condition": "R.b = S.b", "left": {...}, "right": {...}}
    {"op": "cross",     "left": {...}, "right": {...}}
    {"op": "union",     "left": {...}, "right": {...}}       also union_all, intersect, except

from_json rejects anything else with a PlanError that names the offending node.
"""
import json
from .ast import RelationRef, UnaryOp, BinaryOp
from .condition import parse_condition, condition_text
from .errors import RAError, PlanError
from .operators import attr_text, AGGREGATES
from .parser import Parser
from .tokenizer import Tokenizer

# JSON name -> (operator in the tree, 'all' for the bag version). intersect_all and except_all
# are not in the Module 3 list; they exist so that every tree can be written.
SET_OPS = {
    'union': ('union', None), 'union_all': ('union', 'all'),
    'intersect': ('intersect', None), 'intersect_all': ('intersect', 'all'),
    'except': ('minus', None), 'except_all': ('minus', 'all'),
}
JSON_SET_OPS = {tree: name for name, tree in SET_OPS.items()}

# The fields each operator must carry besides "op", with the Python type each must have.
UNARY = {'child': dict}
BINARY = {'left': dict, 'right': dict}
FIELDS = {
    'scan': {'table': str},
    'select': {'condition': str, **UNARY},
    'project': {'columns': list, **UNARY},
    'rename': {'to': str, **UNARY},
    'distinct': UNARY,
    'group_by': {'keys': list, 'aggregates': list, **UNARY},
    'sort': {'keys': list, **UNARY},
    'limit': {'count': int, **UNARY},
    'join': {'condition': str, **BINARY},
    'cross': BINARY,
    **{name: BINARY for name in SET_OPS},
}
LOGICAL_OPS = tuple(FIELDS)
TYPE_NAMES = {str: 'a string', dict: 'an object', list: 'a list', int: 'an integer'}


def to_json(node):
    if isinstance(node, RelationRef):
        return {'op': 'scan', 'table': node.name}
    if isinstance(node, UnaryOp):
        child = to_json(node.child)
        if node.op == 'select':
            return {'op': 'select', 'condition': condition_text(node.arg), 'child': child}
        if node.op == 'project':
            columns = [[attr_text(c[0]), c[1]] if isinstance(c, tuple) else attr_text(c) for c in node.arg]
            return {'op': 'project', 'columns': columns, 'child': child}
        if node.op == 'rename':
            return {'op': 'rename', 'to': node.arg, 'child': child}
        if node.op == 'group_by':
            keys, aggregates = node.arg
            return {'op': 'group_by', 'keys': [attr_text(k) for k in keys],
                    'aggregates': [[func, attr_text(arg) if arg else '*', name] for func, arg, name in aggregates],
                    'child': child}
        if node.op == 'sort':
            return {'op': 'sort', 'keys': [[attr_text(a), direction] for a, direction in node.arg], 'child': child}
        if node.op == 'limit':
            return {'op': 'limit', 'count': node.arg, 'child': child}
        return {'op': 'distinct', 'child': child}
    left, right = to_json(node.left), to_json(node.right)
    if node.op == 'join':
        return {'op': 'join', 'condition': condition_text(node.arg), 'left': left, 'right': right}
    if node.op == 'times':
        return {'op': 'cross', 'left': left, 'right': right}
    return {'op': JSON_SET_OPS[(node.op, node.arg)], 'left': left, 'right': right}


def from_json(obj):
    if not isinstance(obj, dict):
        raise PlanError(f'A plan node must be an object, found {json.dumps(obj)}')
    op = obj.get('op')
    if not isinstance(op, str):
        raise PlanError("Node without a string field 'op': " + json.dumps(obj)[:80])
    if op not in FIELDS:
        raise PlanError(f"Node '{op}': unknown operator '{op}'")
    for field, typ in FIELDS[op].items():
        if field not in obj:
            raise PlanError(f"Node '{op}': missing required field '{field}'")
        if not isinstance(obj[field], typ) or isinstance(obj[field], bool):
            raise PlanError(f"Node '{op}': field '{field}' must be {TYPE_NAMES[typ]}")
    for field in obj:
        if field != 'op' and field not in FIELDS[op]:
            raise PlanError(f"Node '{op}': unexpected field '{field}'")

    if op == 'scan':
        return RelationRef(obj['table'])
    if op in SET_OPS or op in ('join', 'cross'):
        left, right = from_json(obj['left']), from_json(obj['right'])
        if op == 'join':
            return BinaryOp('join', left, right, _condition(op, obj['condition']))
        if op == 'cross':
            return BinaryOp('times', left, right)
        return BinaryOp(SET_OPS[op][0], left, right, SET_OPS[op][1])
    child = from_json(obj['child'])
    if op == 'select':
        return UnaryOp('select', child, _condition(op, obj['condition']))
    if op == 'project':
        if not obj['columns']:
            raise PlanError("Node 'project': field 'columns' cannot be empty")
        return UnaryOp('project', child, [_project_column(c) for c in obj['columns']])
    if op == 'rename':
        return UnaryOp('rename', child, obj['to'])
    if op == 'group_by':
        keys = [_column(op, k) for k in obj['keys']]
        return UnaryOp('group_by', child, (keys, [_aggregate(a) for a in obj['aggregates']]))
    if op == 'sort':
        return UnaryOp('sort', child, [_sort_key(k) for k in obj['keys']])
    if op == 'limit':
        if obj['count'] < 0:
            raise PlanError("Node 'limit': field 'count' cannot be negative")
        return UnaryOp('limit', child, obj['count'])
    return UnaryOp('distinct', child)


def _condition(op, text):
    try:
        return parse_condition(text)
    except RAError as e:
        raise PlanError(f"Node '{op}': field 'condition' is not a valid condition: {e}") from None


def _column(op, text):
    """"R.a" or "a" -> Attr, read by the parser's own attribute rule."""
    if not isinstance(text, str):
        raise PlanError(f"Node '{op}': a column must be a string, found {json.dumps(text)}")
    try:
        parser = Parser(Tokenizer(text).scan())
        attr = parser.attr()
        parser.expect('EOF', what='end of column name')
        return attr
    except RAError as e:
        raise PlanError(f"Node '{op}': '{text}' is not a column name: {e}") from None


def _project_column(item):
    if isinstance(item, list):
        if len(item) != 2 or not isinstance(item[1], str):
            raise PlanError("Node 'project': a renamed column must be [column, new name]")
        return (_column('project', item[0]), item[1])
    return _column('project', item)


def _aggregate(item):
    if not isinstance(item, list) or len(item) != 3 or not all(isinstance(x, str) for x in item):
        raise PlanError("Node 'group_by': an aggregate must be [function, column or \"*\", output name]")
    func, arg, name = item
    if func not in AGGREGATES:
        raise PlanError(f"Node 'group_by': unknown aggregate '{func}'")
    return (func, None if arg == '*' else _column('group_by', arg), name)


def _sort_key(item):
    if not isinstance(item, list) or len(item) != 2 or item[1] not in ('asc', 'desc'):
        raise PlanError("Node 'sort': a key must be [column, \"asc\" or \"desc\"]")
    return (_column('sort', item[0]), item[1])


def dumps(node, indent=2):
    return json.dumps(to_json(node), indent=indent)


def loads(text):
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError(f'Plan is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}') from None
    return from_json(obj)
