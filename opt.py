#!/usr/bin/env python3
"""Query optimizer command line, Module 4.

    python opt.py analyze  --catalog DIR --table R [--buckets 20]      statistics into the catalog
    python opt.py plan     --catalog DIR --logical q.json --frames 32 --json      the physical plan
    python opt.py explain  --catalog DIR --logical q.json --frames 32
    python opt.py dp-table --catalog DIR --logical q.json [--frames 32]

--catalog and --db are the same option, the database directory; without it the directory is db.

explain prints one line per node of the physical plan, with the estimated rows out of it and
the estimated cost of that operator:

    hash_join (R.b = S.b)        est_rows=2000   est_cost=90
      filter (R.a > 500)         est_rows=1000   est_cost=0
        seqscan R                est_rows=10000  est_cost=200
      seqscan S                  est_rows=500    est_cost=10

est_cost is the operator's cost formula applied to the estimated pages of its inputs: M for a
scan, 0 for a filter or a projection, 3 (M + N) for a hash join (here the filter leaves 20 pages
and S is 10). The formulas of the joins and of the sort include reading their inputs, so the
lines are each checkable by hand and do not add up to the cost of the plan.

    --subtree    est_cost of each line is the cost of everything from that line down; the top
                 line is the cost of the plan, the number the join enumeration compares
    --own        est_cost of each line is its share of the plan's cost (its subtree minus its
                 inputs' subtrees); these lines add up to the cost of the plan
    --rewrite    print the logical plan before and after the rewrite rules, not the physical plan

dp-table prints, for every join block, one row per subset of its inputs: the cheapest way found
to join that subset, its estimated rows and pages, and its cost. --alternatives adds every other
way that was costed.

Options of plan, explain and dp-table:
    --sql "SELECT ..."     in place of --logical: compile the query with the SQL front end first
    --no-rule N            leave out rewrite rule N (1 to 7); may be repeated
    --naive                (plan only) the plan with no optimization at all: operators where the
                           query put them, joins in written order, block nested loop everywhere
    --assume FILE          a JSON file {"rows": {"e": 300, "d,e": 1200}, "pages": {...}} of estimates
                           to use in place of the computed ones, for inputs of a join block and
                           for sets of them. Names are aliases, joined by commas in alphabetical
                           order. It answers "what would be chosen if the estimates were these".

analyze also takes --frames (default 32), the memory its sorts may use, and --all for every table.
--frames of the other commands is the B the plan is costed for (default 32); it reads only the
catalog.
"""
import argparse
import json
import sys
from executor.context import ExecError
from optimizer.planner import Planner, PlanningError, naive
from optimizer.stats import analyze, DEFAULT_BUCKETS
from sqlfront.catalog import CatalogError as SqlCatalogError, Catalog as SqlCatalog, ColumnDef
from sqlfront.errors import SqlError
from sqlfront.translate import compile_sql
from store.buffer import BufferPoolError
from store.catalog import CatalogError
from store.db import Database, is_database


def cmd_analyze(a):
    db = Database(a.db, a.frames)
    try:
        tables = db.catalog.list_tables() if a.all else [a.table]
        if not a.all and a.table is None:
            raise ExecError('analyze needs --table NAME or --all')
        for table in tables:
            stats = analyze(db, table, a.buckets)
            io = stats['io']
            print(f"{table}: n_tuples={stats['n_tuples']} n_pages={stats['n_pages']} width={stats['width']}  "
                  f"(page reads {io['page_reads']}, writes {io['page_writes']})")
            for name, c in stats['columns'].items():
                print(f"  {name}: ndv={c['ndv']} min={c['min']!r} max={c['max']!r} buckets={len(c['hist'])}")
    finally:
        db.close()


def load_logical(a, db):
    if a.sql is not None:
        catalog = SqlCatalog({name: [ColumnDef(*column) for column in db.catalog.get_table(name).columns]
                              for name in db.catalog.list_tables()})
        plans = compile_sql(a.sql, catalog)
        if len(plans) != 1:
            raise ExecError('--sql takes exactly one query')
        return plans[0]
    if a.logical is None:
        raise ExecError('give the logical plan with --logical FILE, or a query with --sql')
    with open(a.logical, encoding='utf8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise ExecError(f'{a.logical} is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}') from None


def cmd_plan(a):
    db = Database(a.db, 8)
    try:
        logical = load_logical(a, db)
        if a.command == 'plan' and a.naive:
            print(json.dumps(naive(logical), indent=2))
            return
        assume = None
        if a.assume:
            with open(a.assume, encoding='utf8') as f:
                assume = json.load(f)
        result = Planner(db.catalog, a.frames, a.no_rule or (), assume).optimize(logical)
        if a.command == 'plan':
            print(json.dumps(result.plan.to_json(), indent=2))
        elif a.command == 'dp-table':
            print(result.dp_text(a.alternatives))
        elif a.rewrite:
            print(result.rewrite_text())
        else:
            print(result.explain(subtree=a.subtree, own=a.own))
    finally:
        db.disk.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog='opt', description='Query optimizer: logical plan in, physical plan out')
    commands = p.add_subparsers(dest='command', required=True)
    c = commands.add_parser('analyze')
    c.add_argument('--db', '--catalog', dest='db', default='db', metavar='DIR')
    c.add_argument('--table')
    c.add_argument('--all', action='store_true')
    c.add_argument('--buckets', type=int, default=DEFAULT_BUCKETS)
    c.add_argument('--frames', type=int, default=32)
    for name in ('plan', 'explain', 'dp-table'):
        c = commands.add_parser(name)
        c.add_argument('--db', '--catalog', dest='db', default='db', metavar='DIR')
        c.add_argument('--logical', metavar='FILE')
        c.add_argument('--sql')
        c.add_argument('--frames', type=int, default=32)
        c.add_argument('--no-rule', type=int, action='append', choices=range(1, 8), metavar='N')
        c.add_argument('--assume', metavar='FILE')
        if name == 'plan':
            c.add_argument('--json', action='store_true')
            c.add_argument('--naive', action='store_true')
        if name == 'explain':
            c.add_argument('--subtree', action='store_true')
            c.add_argument('--own', action='store_true')
            c.add_argument('--rewrite', action='store_true')
        if name == 'dp-table':
            c.add_argument('--alternatives', action='store_true')
    a = p.parse_args(argv)
    try:
        if not is_database(a.db):
            raise ExecError(f"there is no database in '{a.db}'")
        if a.command == 'analyze':
            cmd_analyze(a)
        else:
            cmd_plan(a)
        return 0
    except SqlError as e:
        print(e)
        return 1
    except (ExecError, PlanningError, BufferPoolError, CatalogError, SqlCatalogError, KeyError, TypeError) as e:
        print(f'error: {e}')
        return 1
    except OSError as e:
        print(f'error: cannot read {e.filename}: {e.strerror}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
