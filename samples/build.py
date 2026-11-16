"""A small database to run the tools on by hand, and the one the sample plans and queries use.

    python -m samples.build              builds it in ./db, the directory every tool uses by default
    python -m samples.build --db DIR     somewhere else
    python -m samples.build --force      replace a database that is already there

No dataset, plan file or SQL file was published with the specifications, so these are mine. The
database is built with the tools themselves, exactly as it would be by hand: for each table the
rows are written to samples/data/<table>.csv, then

    storage create-table --name R --schema "a:INT,b:VARCHAR(20),c:INT"
    storage load --table R --file samples/data/R.csv
    opt analyze --table R

The tables:

    R      a:INT, b:VARCHAR(20), c:INT       3,000 rows, 13 pages. The schema of the handout, and
    S      b:VARCHAR(20), d:INT                400 rows,  2 pages. the R and S of its plan example.
    Emp    eid, name, age, salary, did, mgr, city        600 rows. The Emp and Dept of the Module 3
    Dept   did, title, budget, city                       20 rows. and Module 4 examples.
    DupL   b:INT     1, 2, 2, 3          the duplicate-key example of Module 2, Section 2.8:
    DupR   b:INT     2, 2, 4             joined on b they give four rows
    Empty  a:INT, b:VARCHAR(10)          no rows
    Wide   k:INT, v:INT, pad:VARCHAR(200)   1,520 rows, 80 pages: 20 x B at B = 4
    Wide2  k:INT, v:INT, pad:VARCHAR(200)     380 rows, 20 pages

Everything is computed from the row number, so the database is the same every time.

    samples/plans/*.json       one physical plan per operator of Module 2; run with
                               python exec.py run --plan samples/plans/hash_join.json --frames 32 --json
    samples/plans/bad/*.json   plans that must be rejected, one fault each
    samples/section5.sql       the queries of Module 3, Section 5
"""
import argparse
import contextlib
import csv
import io
import os
import shutil
import sys
import opt
import storage
from store.db import is_database

CITIES = ['Ottawa', 'Toronto', 'Hull', 'Kingston', 'Montreal', 'Halifax']


def tables():
    """(name, schema, rows) for every table."""
    yield 'R', 'a:INT,b:VARCHAR(20),c:INT', [(i, f'k{(i * 37) % 200}', (i * 7) % 1000) for i in range(3000)]
    yield 'S', 'b:VARCHAR(20),d:INT', [(f'k{(i * 11) % 250}', i) for i in range(400)]
    yield ('Emp', 'eid:INT,name:VARCHAR(20),age:INT,salary:INT,did:INT,mgr:INT,city:VARCHAR(20)',
           [(i, f'emp{i}', 20 + (i * 7) % 51, 100 + (i * 37) % 4900, i % 20, (i * 13) % 600, CITIES[i % 6])
            for i in range(600)])
    yield ('Dept', 'did:INT,title:VARCHAR(20),budget:INT,city:VARCHAR(20)',
           [(i, f'dept{i}', 100 * i, CITIES[(i * 5) % 6] if i % 4 else 'Gatineau') for i in range(20)])
    yield 'DupL', 'b:INT', [(1,), (2,), (2,), (3,)]
    yield 'DupR', 'b:INT', [(2,), (2,), (4,)]
    yield 'Empty', 'a:INT,b:VARCHAR(10)', []
    yield ('Wide', 'k:INT,v:INT,pad:VARCHAR(200)',
           [((i * 611) % 1520, i % 40, f'{i:06d}' + 'w' * 194) for i in range(1520)])
    yield ('Wide2', 'k:INT,v:INT,pad:VARCHAR(200)',
           [((i * 17) % 380, i % 40, f'{i:06d}' + 'x' * 194) for i in range(380)])


def run(tool, args, say):
    """Call one of the command line tools in this process and pass on what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = tool(args)
    for line in out.getvalue().splitlines():
        say(line)
    if code:
        raise SystemExit(f"{' '.join(args)} failed")


def build(directory, data_dir, say=print):
    os.makedirs(data_dir, exist_ok=True)
    for name, schema, rows in tables():
        path = os.path.join(data_dir, name + '.csv')
        with open(path, 'w', newline='', encoding='utf8') as f:
            csv.writer(f).writerows(rows)
        run(storage.main, ['create-table', '--db', directory, '--name', name, '--schema', schema], say)
        if rows:
            run(storage.main, ['load', '--db', directory, '--table', name, '--file', path], say)
        run(opt.main, ['analyze', '--db', directory, '--table', name], lambda line: None)
    say(f'analyzed {len(list(tables()))} tables; the database is in {directory}')


def main(argv=None):
    p = argparse.ArgumentParser(prog='samples.build', description='Build the sample database')
    p.add_argument('--db', default='db', metavar='DIR')
    p.add_argument('--data', default=os.path.join('samples', 'data'), metavar='DIR', help='where the CSV files go')
    p.add_argument('--force', action='store_true', help='replace a database that is already in DIR')
    a = p.parse_args(argv)
    if os.path.isdir(a.db) and os.listdir(a.db):
        if not a.force:
            print(f"error: '{a.db}' is not empty; choose another --db, or pass --force to replace it")
            return 1
        if not is_database(a.db):
            print(f"error: '{a.db}' is not a database of this system, so --force will not remove it")
            return 1
        shutil.rmtree(a.db)
    build(a.db, a.data)
    return 0


if __name__ == '__main__':
    sys.exit(main())
