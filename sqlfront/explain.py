"""Indented text for the two trees of the front end. Both return a string; nothing prints here.

    syntax_tree(statement)   the parsed query, names as written, no catalog needed
    plan_tree(plan)          a logical plan, one node per line, children indented
"""
import json
from .nodes import (CreateTable, Insert, SetOp, TableRef, SubqueryRef, Column, Lit, Aggregate,
                    Comparison, BoolOp)

LEVEL = {'or': 1, 'and': 2, 'not': 3, 'comparison': 4}


def operand_sql(x):
    if isinstance(x, Lit):
        return str(x.value) if isinstance(x.value, int) else "'" + x.value.replace("'", "''") + "'"
    if isinstance(x, Aggregate):
        return f"{x.func.upper()}({'*' if x.arg is None else operand_sql(x.arg)})"
    return f'{x.table}.{x.name}' if x.table else x.name


def condition_sql(c, minimum=0):
    """A condition as SQL text, with parentheses only where the grouping needs them."""
    if isinstance(c, Comparison):
        level, text = LEVEL['comparison'], f'{operand_sql(c.left)} {c.op} {operand_sql(c.right)}'
    elif isinstance(c, BoolOp):
        level = LEVEL[c.op]
        text = f'{condition_sql(c.left, level)} {c.op.upper()} {condition_sql(c.right, level + 1)}'
    else:
        level, text = LEVEL['not'], 'NOT ' + condition_sql(c.child, LEVEL['not'])
    return f'({text})' if level < minimum else text


def syntax_tree(node, indent=''):
    lines = []
    inner = indent + '  '
    if isinstance(node, CreateTable):
        lines.append(f'{indent}create_table {node.name}')
        for name, kind, size, _ in node.columns:
            lines.append(f"{inner}{name} {kind if size is None else f'{kind}({size})'}")
    elif isinstance(node, Insert):
        lines.append(f"{indent}insert into {node.table}: {', '.join(operand_sql(v) for v in node.values)}")
    elif isinstance(node, SetOp):
        lines.append(indent + node.op)
        lines.append(syntax_tree(node.left, inner))
        lines.append(syntax_tree(node.right, inner))
    else:
        lines.append(indent + ('select distinct' if node.distinct else 'select'))
        if node.items is None:
            lines.append(f'{inner}columns: *')
        else:
            lines.append(f'{inner}columns: ' + ', '.join(
                operand_sql(i.expr) + (f' AS {i.alias}' if i.alias else '') for i in node.items))
        lines.append(f'{inner}from:')
        lines.append(source_tree(node.source, inner + '  '))
        if node.where is not None:
            lines.append(f'{inner}where: {condition_sql(node.where)}')
        if node.group_by:
            lines.append(f"{inner}group by: {', '.join(operand_sql(c) for c in node.group_by)}")
        if node.having is not None:
            lines.append(f'{inner}having: {condition_sql(node.having)}')
        if node.order_by:
            lines.append(f'{inner}order by: ' + ', '.join(
                operand_sql(o.column) + (' DESC' if o.descending else ' ASC') for o in node.order_by))
        if node.limit is not None:
            lines.append(f'{inner}limit: {node.limit}')
    return '\n'.join(lines)


def source_tree(node, indent):
    if isinstance(node, TableRef):
        return f'{indent}table {node.name}' + (f' AS {node.alias}' if node.alias else '')
    if isinstance(node, SubqueryRef):
        return f'{indent}subquery AS {node.alias}\n' + syntax_tree(node.query, indent + '  ')
    head = 'cross' if node.kind == 'cross' else f'join ON {condition_sql(node.condition)}'
    return '\n'.join([indent + head, source_tree(node.left, indent + '  '), source_tree(node.right, indent + '  ')])


def plan_tree(plan, indent=''):
    op = plan['op']
    parts = [op]
    for field, value in plan.items():
        if field in ('op', 'child', 'left', 'right'):
            continue
        if op in ('scan', 'rename', 'limit', 'create_table', 'insert') and not isinstance(value, list):
            parts.append(str(value))
        else:
            text = json.dumps(value, separators=(',', ':'))
            parts.append(f'{field} {text}' if op == 'group_by' else text)
    lines = [indent + ' '.join(parts)]
    for field in ('child', 'left', 'right'):
        if field in plan:
            lines.append(plan_tree(plan[field], indent + '  '))
    return '\n'.join(lines)
