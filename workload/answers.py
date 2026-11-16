"""The answers to the twenty five workload queries, as files, and a comparison of two sets of them.

    python -m workload.answers --db wdb [--frames 32] [--out workload/answers]
        runs every query of workload/queries.sql through the whole system (SQL front end,
        optimizer, executor, storage) and writes each answer to <out>/wNN.csv

    python -m workload.answers --compare workload/answers workload/postgres
        compares two directories of such files, query by query, and exits with 1 if any differ.
        workload/postgres is what  psql -f workload/postgres_queries.sql  writes.

How two answers are compared. A query with ORDER BY must give the same rows in the same order.
Any other query must give the same bag of rows: the same rows the same number of times, in any
order, because SQL promises no order there. Before comparing, every value is put in one form:
an integer as written, any other number to four decimal places (this system prints AVG that
way, PostgreSQL prints it with sixteen), anything else as the text it is.
"""
import argparse
import csv
import os
import sys
from executor.run import Execution
from optimizer.planner import Planner
from sqlfront.catalog import load_catalog
from sqlfront.translate import compile_sql
from store.db import Database, is_database
from workload.build import queries


def normal(value):
    """One value in the form used for comparison."""
    text = f'{value:.4f}' if isinstance(value, float) else str(value)
    body = text[1:] if text[:1] in '+-' else text
    if body.isdigit():
        return str(int(text))
    whole, dot, fraction = body.partition('.')
    if dot and whole.isdigit() and fraction.isdigit():
        return f'{float(text):.4f}'
    return text


def run(db_dir, frames):
    """[(sql, rows)] for every query, rows as lists of normalised values."""
    catalog = load_catalog(db_dir)
    out = []
    for sql in queries():
        logical = compile_sql(sql, catalog)[0]
        db = Database(db_dir, frames)
        try:
            physical = Planner(db.catalog, frames).optimize(logical).plan.to_json()
        finally:
            db.disk.close()
        db = Database(db_dir, frames)
        try:
            rows = [[normal(v) for v in row] for row in Execution(db, physical).rows()]
        finally:
            db.disk.close()
        out.append((sql, rows))
    return out


def read(path):
    with open(path, newline='', encoding='utf8') as f:
        return [[normal(v) for v in row] for row in csv.reader(f)]


def same_answer(sql, ours, theirs):
    if ' ORDER BY ' in sql.upper():
        return ours == theirs
    return sorted(ours) == sorted(theirs)


def compare(ours_dir, theirs_dir):
    different = 0
    for number, sql in enumerate(queries(), start=1):
        name = f'w{number:02d}.csv'
        paths = [os.path.join(d, name) for d in (ours_dir, theirs_dir)]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            print(f'W{number:02d}  MISSING    {missing[0]}')
            different += 1
            continue
        ours, theirs = read(paths[0]), read(paths[1])
        if same_answer(sql, ours, theirs):
            print(f'W{number:02d}  same       {len(ours)} rows')
        else:
            different += 1
            print(f'W{number:02d}  DIFFERENT  {len(ours)} rows in {ours_dir}, {len(theirs)} in {theirs_dir}')
    print(f'{len(queries()) - different} of {len(queries())} the same')
    return 1 if different else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='workload.answers')
    p.add_argument('--db', default='wdb', metavar='DIR')
    p.add_argument('--frames', type=int, default=32)
    p.add_argument('--out', default=os.path.join('workload', 'answers'), metavar='DIR')
    p.add_argument('--compare', nargs=2, metavar='DIR')
    a = p.parse_args(argv)
    if a.compare:
        return compare(*a.compare)
    if not is_database(a.db):
        print(f"error: there is no database in '{a.db}'; build it with workload/load.sql")
        return 1
    os.makedirs(a.out, exist_ok=True)
    for number, (_, rows) in enumerate(run(a.db, a.frames), start=1):
        with open(os.path.join(a.out, f'w{number:02d}.csv'), 'w', newline='', encoding='utf8') as f:
            csv.writer(f, lineterminator='\n').writerows(rows)
        print(f'W{number:02d}  {len(rows)} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
