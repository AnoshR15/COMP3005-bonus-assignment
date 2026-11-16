"""The input files of the integration workload.

    python -m workload.build

writes
    workload/data/<Table>.csv        the six tables of the order database, one row per line, no
                                     header: what LOAD reads here and what \\copy reads in PostgreSQL
    workload/postgres_queries.sql    the twenty five queries of workload/queries.sql, each wrapped
                                     so that PostgreSQL writes its answer to workload/postgres/wNN.csv

No dataset and no queries were published for the integration checkpoint, so both are mine. The
data is the order database REPORT_M4.md was measured on, row for row (bench.m4_measure.order_tables,
one fixed seed). The queries are new. REPORT_INTEGRATION.md documents both.

The whole procedure:

    python -m workload.build
    python minidb.py --db wdb --file workload/load.sql
    python minidb.py --db wdb --file workload/queries.sql --report      the table of the report
    python -m workload.answers --db wdb                                 this system's answers, as CSV

and, where PostgreSQL is installed:

    createdb --template=template0 --locale=C workload
    psql -d workload -f workload/postgres_load.sql
    psql -d workload -f workload/postgres_queries.sql
    python -m workload.answers --compare workload/answers workload/postgres
"""
import csv
import os
import sys
from bench.m4_measure import order_tables
from minidb import split_statements, strip_comments

HERE = os.path.dirname(os.path.abspath(__file__))


def queries():
    """The statements of workload/queries.sql, each on one line and without its comments."""
    with open(os.path.join(HERE, 'queries.sql'), encoding='utf8') as f:
        return [' '.join(strip_comments(s).split()) for s in split_statements(f.read())]


def main():
    data = os.path.join(HERE, 'data')
    os.makedirs(data, exist_ok=True)
    for name, _, rows in order_tables():
        with open(os.path.join(data, name + '.csv'), 'w', newline='', encoding='utf8') as f:
            csv.writer(f, lineterminator='\n').writerows(rows)
        print(f'{name}: {len(rows)} rows')
    lines = ['-- Written by python -m workload.build from workload/queries.sql. Do not edit.',
             '-- psql -d workload -f workload/postgres_queries.sql   (from the project root)',
             '\\! mkdir -p workload/postgres', '']
    for number, sql in enumerate(queries(), start=1):
        lines.append(f"\\copy ({sql}) TO 'workload/postgres/w{number:02d}.csv' WITH (FORMAT csv)")
    with open(os.path.join(HERE, 'postgres_queries.sql'), 'w', newline='\n', encoding='utf8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'{len(queries())} queries in workload/postgres_queries.sql')
    return 0


if __name__ == '__main__':
    sys.exit(main())
