#!/usr/bin/env python3
"""Execution engine command line, Module 2.

    python exec.py run   --plan plan.json --frames 32 --json
    python exec.py trace --plan plan.json --frames 32

The database is the directory db unless --db DIR (or --catalog DIR, the same option) names another.

run prints the rows. With --json:

    {"schema": [["R.a","INT"],["S.c","VARCHAR",20]],
     "rows": [[42,"bob"]],
     "totals": {"tuples_out":1,"comparisons":4000,"page_reads":120,"page_writes":0}}

trace runs the plan, discards the rows, and prints one line per operator, in plan order,
indented by depth:

    hash_join          tuples_out=1     comparisons=4000  reads=96  writes=48
      seqscan R        tuples_out=200   comparisons=0     reads=24  writes=0
      seqscan S        tuples_out=150   comparisons=0     reads=24  writes=0

tuples_out and comparisons are the operator's own. reads and writes are the page reads and
writes of the operator and of everything below it, so a join's line is the cost of the join the
way the textbook formula counts it, reading its inputs included: 96 = 24 + 24 by the two scans
+ 48 read back from the partitions. The top line is the whole plan and equals the totals of run.

    --own        reads and writes of each line are that operator's alone (the join above: 48)
    --verbose    also the frames each operator held, its details, and the peak frames in use
    --json       all of it as one JSON object, with both kinds of page count for every operator

--frames is the memory budget B: the buffer pool has exactly that many frames, and they are the
only memory a plan has. --policy lru|clock picks the replacement policy (default lru).
An error is one line starting with "error:" and the exit code is 1.

A plan is opened and its first row fetched before run prints anything, so a plan that is rejected
or fails at the start prints that one line and nothing else. A plan can also fail after rows
have been written (the second input of a union_all is opened late). With --json the output is
then still one valid JSON object: the rows that came out, an "error" member with the message,
the totals so far; the exit code is 1 and the error line goes to standard error.
"""
import argparse
import json
import sys
from executor.context import ExecError
from executor.run import Execution, load_plan, format_row
from store.buffer import BufferPoolError
from store.catalog import CatalogError
from store.db import Database, is_database


def main(argv=None):
    p = argparse.ArgumentParser(prog='exec', description='Execution engine: runs a physical plan')
    commands = p.add_subparsers(dest='command', required=True)
    for name in ('run', 'trace'):
        c = commands.add_parser(name)
        c.add_argument('--db', '--catalog', dest='db', default='db', metavar='DIR',
                       help='database directory (default: db)')
        c.add_argument('--plan', required=True, metavar='FILE', help='physical plan, JSON')
        c.add_argument('--frames', type=int, required=True, help='memory budget B, in frames')
        c.add_argument('--policy', choices=['lru', 'clock'], default='lru')
        c.add_argument('--json', action='store_true')
        if name == 'trace':
            c.add_argument('--own', action='store_true', help="each line's page counts are that operator's alone")
            c.add_argument('--subtree', action='store_true', help='the default; accepted for old command lines')
            c.add_argument('--verbose', action='store_true')
    a = p.parse_args(argv)
    db = None
    try:
        if not is_database(a.db):
            raise ExecError(f"there is no database in '{a.db}'")
        with open(a.plan, encoding='utf8') as f:
            plan = load_plan(f.read())
        db = Database(a.db, a.frames, a.policy)
        execution = Execution(db, plan)
        if a.command == 'run':
            return run(execution, a.json)
        else:
            for _ in execution.rows():
                pass
            if a.json:
                print(json.dumps(execution.report(), indent=2))
            else:
                print('\n'.join(execution.trace(own=a.own)))
                if a.verbose:
                    verbose(execution)
        return 0
    except (ExecError, BufferPoolError, CatalogError) as e:
        print(f'error: {e}')
        return 1
    except OSError as e:
        print(f'error: cannot read {e.filename}: {e.strerror}')
        return 1
    finally:
        if db is not None:
            db.disk.close()                 # a query changes nothing, so there is nothing to flush


def run(execution, as_json):
    rows = execution.started()              # an error in open() or on the first row is raised here,
    if not as_json:                         # before the header: the output is then one error line
        print(', '.join(c[0] for c in execution.schema()))
        count = 0
        for row in rows:
            print(', '.join(repr(v) if isinstance(v, str) else f'{v:.4f}' if isinstance(v, float) else str(v)
                            for v in row))
            count += 1
        totals = execution.totals()
        print(f'({count} rows)  ' + '  '.join(f'{k}={v}' for k, v in totals.items()), file=sys.stderr)
        return 0
    # Rows are written as they arrive: the result is never collected in memory.
    out = sys.stdout
    out.write('{"schema": ' + json.dumps(execution.schema()) + ',\n "rows": [')
    first = True
    failure = None
    try:
        for row in rows:
            out.write(('\n  ' if first else ',\n  ') + format_row(row))
            first = False
    except (ExecError, BufferPoolError) as e:
        # A plan can still fail after its first row: the right input of a union_all is opened
        # only when the left one is finished. The rows already written cannot be taken back, so
        # the object is closed properly and says what happened: valid JSON, an "error" member,
        # exit code 1, and the same line on standard error.
        failure = e
    out.write(('' if first else '\n ') + '],\n')
    if failure is not None:
        out.write(' "error": ' + json.dumps(str(failure)) + ',\n')
        print(f'error: {failure}', file=sys.stderr)
    out.write(' "totals": ' + json.dumps(execution.totals()) + '}\n')
    return 1 if failure is not None else 0


def verbose(execution):
    print()
    def short(value):
        if isinstance(value, list) and value and isinstance(value[0], dict):    # overflow events: count them
            actions = [item.get('action', '?') for item in value]
            return ', '.join(f'{actions.count(a)} x {a}' for a in sorted(set(actions)))
        return value

    for depth, op in execution.operators():
        details = '  '.join(f'{k}={short(v)}' for k, v in op.details().items())
        print(f"{'  ' * depth}{op.label}: holds {op.hold} frames while streaming" + (f'; {details}' if details else ''))
    t = execution.totals()
    print(f"\ntotals: tuples_out={t['tuples_out']} comparisons={t['comparisons']} "
          f"reads={t['page_reads']} writes={t['page_writes']}")
    print(f'frames: budget {execution.frames}, most in use at once {execution.peak_frames()}')


if __name__ == '__main__':
    sys.exit(main())
