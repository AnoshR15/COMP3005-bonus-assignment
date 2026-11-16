#!/usr/bin/env python3
"""The whole system behind one prompt: SQL in, rows out.

    python minidb.py --db DIR [--frames 32]                 interactive; statements end with ;
    python minidb.py --db DIR --file script.sql             run a file of statements
    python minidb.py --db DIR --file queries.sql --report   one summary line per query, no rows

Without --db the database is the directory db, as for the four module tools.

Statements:

    CREATE TABLE name (col TYPE, ...)       INSERT INTO name VALUES (...)
    SELECT ...                              the SQL subset of Module 3
    EXPLAIN query                           the physical plan, with estimated rows and cost
    EXPLAIN ANALYZE query                   run it: estimated against actual, line by line; cost
                                            and I/O on a line are for everything from it down
    ANALYZE table                           compute statistics (ANALYZE with no table: all tables)
    SET frames = n                          the memory budget B for what follows
    LOAD table FROM 'file.csv'              bulk load a CSV file

This file is wiring and nothing else. A query passes through the four modules in order:

    SQL text  -> sqlfront.compile_sql    -> logical plan      (Module 3)
              -> optimizer.Planner       -> physical plan     (Module 4)
              -> executor.Execution      -> rows              (Module 2)
              -> store.Database          -> pages on disk     (Module 1)

Every statement gets a buffer pool of its own, of `frames` frames, so the page counts printed
for a query do not depend on what ran before it and equal what `exec run` prints for the plan.

--report prints, for each query: result rows, page reads and writes, wall time, the rows the
optimizer estimated at the root against the rows that came out, and its estimated cost.
"""
import argparse
import sys
import time
from executor.context import ExecError
from executor.run import Execution
from optimizer.planner import Planner, PlanningError
from optimizer.stats import analyze
from sqlfront.catalog import Catalog as SqlCatalog, ColumnDef, CatalogError as SqlCatalogError, parse_type
from sqlfront.errors import SqlError
from sqlfront.translate import compile_sql
from store.buffer import BufferPoolError
from store.catalog import CatalogError
from store.db import Database, load_csv
from store.record import RecordError

ERRORS = (ExecError, PlanningError, BufferPoolError, CatalogError, SqlCatalogError, RecordError)


def split_statements(text):
    """Cut a script at the semicolons that are not inside a string or a -- comment."""
    statements, start, i, n = [], 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":                                    # a string: skip to its closing quote
            i += 1
            while i < n and not (text[i] == "'" and text[i + 1:i + 2] != "'"):
                i += 2 if text[i] == "'" else 1
        elif c == '-' and text[i + 1:i + 2] == '-':     # a comment: skip to the end of the line
            while i < n and text[i] != '\n':
                i += 1
        elif c == ';':
            statements.append(text[start:i])
            start = i + 1
        i += 1
    statements.append(text[start:])
    return [s for s in statements if strip_comments(s).strip()]


def strip_comments(text):
    lines = []
    for line in text.split('\n'):
        quoted, i = False, 0
        while i < len(line):
            if line[i] == "'":
                quoted = not quoted
            elif not quoted and line[i:i + 2] == '--':
                break
            i += 1
        lines.append(line[:i])
    return '\n'.join(lines)


def format_value(v):
    return f'{v:.4f}' if isinstance(v, float) else str(v)


class Session:

    def __init__(self, directory, frames, out=sys.stdout):
        self.directory = directory
        self.frames = frames
        self.out = out
        self.last = None            # facts about the most recent query, for --report

    def say(self, text=''):
        print(text, file=self.out)

    def run(self, statement, quiet=False):
        """Execute one statement. Returns True unless it failed; a failure prints one line."""
        self.last = None
        try:
            words = strip_comments(statement).split()
            head = words[0].upper()
            if head == 'SET':
                self.set(words)
            elif head == 'LOAD':
                self.load(words, statement)
            elif head == 'ANALYZE':
                self.analyze(words)
            elif head == 'EXPLAIN':
                run_it = len(words) > 1 and words[1].upper() == 'ANALYZE'
                self.explain(self.blank(statement, 2 if run_it else 1), run_it)
            else:
                self.sql(statement, quiet)
            return True
        except SqlError as e:
            self.say(str(e))
        except ERRORS as e:
            self.say(f'error: {e}')
        except OSError as e:
            self.say(f'error: cannot read {e.filename}: {e.strerror}')
        return False

    @staticmethod
    def blank(statement, count):
        """The statement with its first `count` words replaced by spaces, so that the line and
        column of an error in the query that follows are still those of the original text."""
        out, i = list(statement), 0
        for _ in range(count):
            while out[i].isspace():
                i += 1
            while i < len(out) and not out[i].isspace():
                out[i] = ' '
                i += 1
        return ''.join(out)

    # ---------------------------------------------------------------- the extra commands

    def set(self, words):
        text = ' '.join(words[1:]).replace('=', ' = ').split()
        if len(text) != 3 or text[0].lower() != 'frames' or text[1] != '=' or not text[2].isdigit():
            raise ExecError('expected SET frames = n')
        if int(text[2]) < 3:
            raise ExecError('frames must be at least 3')
        self.frames = int(text[2])
        self.say(f'frames = {self.frames}')

    def load(self, words, statement):
        if len(words) < 4 or words[2].upper() != 'FROM':
            raise ExecError("expected LOAD table FROM 'file'")
        path = statement[statement.upper().index('FROM') + 4:].strip()
        if path[:1] == "'" and path[-1:] == "'":
            path = path[1:-1]
        db = Database(self.directory, self.frames)
        try:
            heap = db.table(words[1])
            count = load_csv(heap, path)
            self.say(f'loaded {count} rows into {words[1]}: {heap.info.num_tuples} rows on '
                     f'{heap.info.num_pages} pages')
        finally:
            db.close()

    def analyze(self, words):
        db = Database(self.directory, self.frames)
        try:
            for table in ([words[1]] if len(words) > 1 else db.catalog.list_tables()):
                stats = analyze(db, table)
                self.say(f"analyzed {table}: {stats['n_tuples']} rows, {stats['n_pages']} pages, "
                         f"{len(stats['columns'])} columns")
        finally:
            db.close()

    # ---------------------------------------------------------------- SQL

    def compile(self, db, text):
        catalog = SqlCatalog({name: [ColumnDef(*column) for column in db.catalog.get_table(name).columns]
                              for name in db.catalog.list_tables()})
        plans = compile_sql(text, catalog)
        if len(plans) != 1:
            raise ExecError('one statement at a time')
        return plans[0]

    def sql(self, statement, quiet):
        db = Database(self.directory, self.frames)
        try:
            plan = self.compile(db, statement)
            if plan['op'] == 'create_table':
                db.create_table(plan['table'], [(name, *parse_type(typ)) for name, typ in plan['columns']])
                db.close()
                return self.say(f"created table {plan['table']}")
            if plan['op'] == 'insert':
                db.table(plan['table']).insert(tuple(plan['values']))
                db.close()
                return self.say('inserted 1 row')
            result = Planner(db.catalog, self.frames).optimize(plan)
        except BaseException:
            db.disk.close()
            raise
        db.disk.close()
        rows, execution, seconds = self.execute(result, None if quiet else self.print_row, quiet)
        totals = execution.totals()
        self.last = {'rows': rows, 'reads': totals['page_reads'], 'writes': totals['page_writes'],
                     'seconds': seconds, 'estimated': result.plan.rows, 'cost': round(result.plan.cost)}
        if not quiet:
            self.say(f"({rows} row{'' if rows == 1 else 's'}; page reads {totals['page_reads']}, "
                     f"writes {totals['page_writes']}; {seconds * 1000:.0f} ms; estimated rows {result.plan.rows})")

    def print_row(self, row):
        self.say(' | '.join(format_value(v) for v in row))

    def execute(self, result, each_row, quiet=False):
        """Run an optimized plan in a fresh buffer pool. -> (row count, Execution, seconds)"""
        db = Database(self.directory, self.frames)
        try:
            execution = Execution(db, result.plan.to_json())
            start = time.perf_counter()
            rows = execution.started()      # a plan that fails, fails here: before the header
            if each_row is not None and not quiet:
                self.say(' | '.join(c[0] for c in execution.schema()))
            count = 0
            for row in rows:
                count += 1
                if each_row is not None:
                    each_row(row)
            return count, execution, time.perf_counter() - start
        finally:
            db.disk.close()

    def explain(self, query, run_it):
        db = Database(self.directory, self.frames)
        try:
            result = Planner(db.catalog, self.frames).optimize(self.compile(db, query))
        finally:
            db.disk.close()
        if not run_it:
            return self.say(result.explain())
        rows, execution, seconds = self.execute(result, None)
        nodes = list(result.plan.walk())
        labels = ['  ' * depth + node.label() for depth, node in nodes]
        width = max(len(label) for label in labels) + 2
        # Like against like: est_cost and actual_io on a line are both for everything from that
        # line down, so the top line is the plan (the same view as exec trace and opt explain --subtree).
        for label, (_, node), (_, op) in zip(labels, nodes, execution.operators()):
            self.say(f'{label:<{width}} est_rows={node.rows:<8} actual_rows={op.c.tuples_out:<8} '
                     f'est_cost={round(node.cost):<6} actual_io={sum(execution.below(op))}')
        totals = execution.totals()
        self.say(f"({rows} row{'' if rows == 1 else 's'}; estimated cost {round(result.plan.cost)}, page reads "
                 f"{totals['page_reads']}, writes {totals['page_writes']}; {seconds * 1000:.0f} ms; "
                 f'most frames in use {execution.peak_frames()} of {self.frames})')


def report(session, statements):
    """One line per statement: what the integration report asks for (result rows, page I/O, wall
    time, estimated against actual rows at the root), and last the optimizer's estimated cost,
    to hold against reads plus writes."""
    session.say(f"{'#':>3}  {'rows':>7}  {'reads':>7}  {'writes':>7}  {'ms':>7}  {'est rows':>9}  "
                f"{'actual':>7}  {'q-error':>7}  {'est cost':>8}")
    for number, statement in enumerate(statements, start=1):
        quiet_out, session.out = session.out, _Capture()
        ok = session.run(statement, quiet=True)
        message, session.out = session.out.text.strip(), quiet_out
        last = session.last
        if not ok:
            session.say(f'{number:>3}  {message}')
        elif last is None:
            session.say(f'{number:>3}  {message}')
        else:
            est, act = max(last['estimated'], 1), max(last['rows'], 1)
            session.say(f"{number:>3}  {last['rows']:>7}  {last['reads']:>7}  {last['writes']:>7}  "
                        f"{last['seconds'] * 1000:>7.0f}  {last['estimated']:>9}  {last['rows']:>7}  "
                        f"{max(est, act) / min(est, act):>7.2f}  {last['cost']:>8}")


class _Capture:
    def __init__(self):
        self.text = ''

    def write(self, s):
        self.text += s


def main(argv=None):
    p = argparse.ArgumentParser(prog='minidb', description='SQL in, rows out')
    p.add_argument('--db', '--catalog', dest='db', default='db', metavar='DIR',
                   help='database directory, created if missing (default: db)')
    p.add_argument('--frames', type=int, default=32, help='memory budget B (default 32)')
    p.add_argument('--file', help='run the statements of this file instead of prompting')
    p.add_argument('--report', action='store_true', help='with --file: one summary line per statement')
    a = p.parse_args(argv)
    if a.frames < 3:
        print('error: --frames must be at least 3')
        return 1
    session = Session(a.db, a.frames)
    if a.file:
        try:
            with open(a.file, encoding='utf8') as f:
                statements = split_statements(f.read())
        except OSError as e:
            print(f'error: cannot read {e.filename}: {e.strerror}')
            return 1
        if a.report:
            report(session, statements)
            return 0
        failed = [s for s in statements if not session.run(s)]
        return 1 if failed else 0
    buffer = ''
    while True:
        try:
            line = input('minidb> ' if not buffer.strip() else '   ...> ')
        except EOFError:
            break
        if not buffer.strip() and line.strip().lower() in ('quit', 'exit', '\\q'):
            break
        buffer += line + '\n'
        statements = split_statements(buffer)
        if ';' in line and statements:
            complete = statements if buffer.rstrip().endswith(';') else statements[:-1]
            for statement in complete:
                session.run(statement)
            buffer = '' if buffer.rstrip().endswith(';') else statements[-1]
    return 0


if __name__ == '__main__':
    sys.exit(main())
