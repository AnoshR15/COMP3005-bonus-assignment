#!/usr/bin/env python3
"""Command line entry point.

    python ra.py --tree "<query>"                     print the parse tree and stop
    python ra.py --json "<query>"                     print the plan as JSON and stop
    python ra.py --run FILE [--run FILE2] "<query>"   load relation files, run the query, print the result

The result goes to standard output: a schema line, then one tuple per line. The tuple count and
the operator counters (tuples examined by select, pairs compared by join) go to standard error.
Every error is printed as one line starting with "Error:", never as a stack trace.

This file is the only place that prints. Everything under source/ is a library.
"""
import argparse
import sys
from source.tree import tree
from source.plan_json import dumps
from source.relation_file import load_relations
from source.engine import evaluate, parse_query, MemorySource
from source.operators import Counters
from source.errors import RAError


def format_value(v):
    return repr(v) if isinstance(v, str) else str(v)


def main(argv=None):
    p = argparse.ArgumentParser(description='Relational algebra engine')
    p.add_argument('--tree', action='store_true', help='print the parse tree without executing')
    p.add_argument('--json', action='store_true', help='print the plan as JSON without executing')
    p.add_argument('--run', metavar='FILE', action='append', help='relation file to load (repeatable)')
    p.add_argument('query')
    a = p.parse_args(argv)
    try:
        ast = parse_query(a.query)
        if a.tree:
            print(tree(ast))
            return 0
        if a.json:
            print(dumps(ast))
            return 0
        if not a.run:
            print('Error: --run FILE is needed to execute a query (use --tree to only print the tree)')
            return 2
        relations = {}
        for path in a.run:
            relations.update(load_relations(path))
        counters = Counters()
        result = evaluate(ast, MemorySource(relations), counters)
        print('Schema: ' + ', '.join(x.qualified() for x in result.schema))
        count = 0
        for row in result.ordered_rows():      # sorted so the output is reproducible
            print(', '.join(format_value(v) for v in row))
            count += 1
        print(f'({count} tuple{"" if count == 1 else "s"})', file=sys.stderr)
        print(f'select examined {counters.select_examined} tuples; '
              f'join compared {counters.join_compared} pairs', file=sys.stderr)
        return 0
    except RAError as e:                       # lexical, syntax, name, schema, type and data errors
        print(f'Error: {e}')
        return 1
    except OSError as e:                       # unreadable relation file
        print(f'Error: cannot read {e.filename}: {e.strerror}')
        return 1
    except Exception as e:                     # a bug in the engine: still one line, never a trace
        print(f'Error: internal error, {type(e).__name__}: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
