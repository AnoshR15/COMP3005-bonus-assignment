#!/usr/bin/env python3
"""SQL front end, Module 3.

    python sql.py parse   --query "SELECT ..." --explain         syntax tree, no catalog needed
    python sql.py compile --db DIR --file q.sql --json           logical plan as JSON
    python sql.py compile --db DIR --file q.sql --explain        logical plan as an indented tree
    python sql.py check   --db DIR --file q.sql                  errors only; exit code 0 or 1

Every command takes the SQL from --query "..." or from --file PATH. A file may hold several
statements separated by semicolons. With one statement, compile --json prints one plan object;
with several it prints a JSON list, one entry per statement.

An error is one line,  error: <category>: <message> at line L, column C,  and the exit code is 1.
Only the first error is reported. This file is the only place that prints or reads the SQL file.
"""
import argparse
import json
import sys
from sqlfront.catalog import load_catalog, CatalogError
from sqlfront.errors import SqlError
from sqlfront.explain import syntax_tree, plan_tree
from sqlfront.parser import parse_sql
from sqlfront.translate import compile_sql


def read_sql(args):
    if args.query is not None:
        return args.query
    with open(args.file, encoding='utf8') as f:
        return f.read()


def main(argv=None):
    p = argparse.ArgumentParser(prog='sql', description='SQL front end: SQL in, logical plan out')
    commands = p.add_subparsers(dest='command', required=True)
    for name in ('parse', 'compile', 'check'):
        c = commands.add_parser(name)
        source = c.add_mutually_exclusive_group(required=True)
        source.add_argument('--query', help='SQL text')
        source.add_argument('--file', help='file of SQL statements')
        if name == 'parse':
            c.add_argument('--explain', action='store_true', help='print the syntax tree')
        else:
            c.add_argument('--db', '--catalog', dest='db', default='db', metavar='DIR',
                           help='database directory holding the catalog (default: db)')
        if name == 'compile':
            c.add_argument('--json', action='store_true', help='print the logical plan as JSON (default)')
            c.add_argument('--explain', action='store_true', help='print the logical plan as a tree')
    a = p.parse_args(argv)
    try:
        text = read_sql(a)
        if a.command == 'parse':
            statements = parse_sql(text)
            if a.explain:
                print('\n\n'.join(syntax_tree(s) for s in statements))
            return 0
        plans = compile_sql(text, load_catalog(a.db))
        if a.command == 'check':
            return 0
        if a.explain:
            print('\n\n'.join(plan_tree(plan) for plan in plans))
        else:
            print(json.dumps(plans[0] if len(plans) == 1 else plans, indent=2))
        return 0
    except SqlError as e:
        print(e)
        return 1
    except CatalogError as e:
        print(f'error: {e}')
        return 1
    except OSError as e:
        print(f'error: cannot read {e.filename}: {e.strerror}')
        return 1
    except Exception as e:                     # a bug in the front end: still one line, never a trace
        print(f'error: internal error, {type(e).__name__}: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
