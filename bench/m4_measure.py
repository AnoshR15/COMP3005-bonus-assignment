"""The measurements of REPORT_M4.md. Every number in that report comes from the files this writes.

    python -m bench.m4_measure                 all four studies, plus the DP table of one query
    python -m bench.m4_measure quality pays    only the named ones: quality pays rules breaks dp

Writes results/m4/*.csv, results/m4/tables.md and a few text files. Builds bench/m4db once.

The data and the queries are mine: no dataset or queries were published with the specification.
This generates a small order database from one fixed seed, and twenty queries. REPORT_M4.md
section 0 gives the schemas, how the rows are made, and each query with what it is there for.
The database has pairs of strongly correlated columns, as the specification says its dataset has:

    Customer.city -> Customer.country     every city lies in exactly one country
    Orders.odate  -> Orders.shipdate      an order ships 1 to 5 days after it is placed
    Product.brand -> Product.category     every brand belongs to exactly one category

Every query goes the whole way: SQL -> Module 3 -> logical plan -> Module 4 -> physical plan ->
Module 2 -> rows. Page I/O is what the executor's disk manager counted.
"""
import csv
import json
import os
import random
import statistics
import sys
import time
from executor.run import Execution
from optimizer.planner import Planner, naive
from optimizer.stats import analyze
from sqlfront.catalog import load_catalog
from sqlfront.translate import compile_sql
from store.db import Database, is_database

DB = os.path.join('bench', 'm4db')
OUT = os.path.join('results', 'm4')
FRAMES = 8                              # B for the workload

COUNTRIES = ['Peru', 'Chile', 'Kenya', 'Japan', 'Norway', 'Canada']
CITIES = ['Lima', 'Cusco', 'Arequipa', 'Piura', 'Ica', 'Santiago', 'Valdivia', 'Arica', 'Talca', 'Osorno',
          'Nairobi', 'Mombasa', 'Kisumu', 'Nakuru', 'Eldoret', 'Tokyo', 'Osaka', 'Kyoto', 'Nagoya', 'Sapporo',
          'Oslo', 'Bergen', 'Tromso', 'Bodo', 'Alta', 'Toronto', 'Ottawa', 'Calgary', 'Halifax', 'Regina']

QUERIES = [
    ('Q1', "SELECT name, city FROM Customer WHERE segment = 2"),
    ('Q2', "SELECT oid, total FROM Orders WHERE odate > 900"),
    ('Q3', "SELECT oid FROM Orders WHERE status = 3 AND total > 4000"),
    ('Q4', "SELECT c.name, o.total, o.note FROM Customer c JOIN Orders o ON c.cid = o.cid WHERE o.total > 2500"),
    ('Q5', "SELECT c.name, o.oid FROM Customer c, Orders o WHERE c.cid = o.cid AND c.segment = 1 AND o.status = 2"),
    ('Q6', "SELECT c.name, o.note, i.note FROM Customer c JOIN Orders o ON c.cid = o.cid "
           "JOIN Item i ON o.oid = i.oid WHERE c.country = 'Peru' AND i.qty > 4"),
    ('Q7', "SELECT status, COUNT(*) AS n, AVG(total) AS avg_total FROM Orders GROUP BY status"),
    ('Q8', "SELECT c.country, COUNT(*) AS n FROM Customer c JOIN Orders o ON c.cid = o.cid GROUP BY c.country"),
    ('Q9', "SELECT cid, COUNT(*) AS n FROM Orders GROUP BY cid HAVING COUNT(*) > 8 AND cid < 100"),
    ('Q10', "SELECT DISTINCT pid FROM Item WHERE qty > 8"),
    ('Q11', "SELECT oid, total FROM Orders WHERE status = 1 ORDER BY total DESC LIMIT 10"),
    ('Q12', "SELECT c.name, p.pname, i.note FROM Item i JOIN Orders o ON i.oid = o.oid "
            "JOIN Customer c ON o.cid = c.cid JOIN Product p ON i.pid = p.pid WHERE p.category < 3 AND c.segment < 3"),
    ('Q13', "SELECT c.name, p.pname, y.cost, o.note FROM Item i JOIN Orders o ON i.oid = o.oid "
            "JOIN Customer c ON o.cid = c.cid JOIN Product p ON i.pid = p.pid JOIN Supply y ON y.pid = p.pid "
            "WHERE y.cost < 20 AND o.status = 4"),
    ('Q14', "SELECT c.name, s.sname FROM Item i JOIN Orders o ON i.oid = o.oid JOIN Customer c ON o.cid = c.cid "
            "JOIN Product p ON i.pid = p.pid JOIN Supply y ON y.pid = p.pid JOIN Supplier s ON s.sid = y.sid "
            "WHERE s.country = 'Chile' AND o.odate < 100"),
    ('Q15', "SELECT a.oid, b.oid FROM Orders a JOIN Orders b ON a.cid = b.cid WHERE a.odate < 15 AND b.odate > 985"),
    ('Q16', "SELECT c.name, t.n FROM (SELECT cid, COUNT(*) AS n FROM Orders GROUP BY cid) AS t "
            "JOIN Customer c ON t.cid = c.cid WHERE t.n > 8 AND t.cid < 150 AND c.segment = 3"),
    ('Q17', "SELECT t.cid FROM (SELECT cid FROM Orders UNION ALL SELECT cid FROM Customer) AS t WHERE t.cid < 20"),
    ('Q18', "SELECT p.pname, i.price FROM Item i JOIN Product p ON i.pid = p.pid AND i.price > p.weight "
            "WHERE p.brand = 7"),
    ('Q19', "SELECT c.name, o.oid FROM Customer c JOIN Orders o ON c.cid = o.cid "
            "WHERE c.city = 'Lima' AND c.country = 'Peru'"),
    ('Q20', "SELECT o.oid, o.note, i.pid, i.note FROM Orders o JOIN Item i ON o.oid = i.oid "
            "WHERE o.odate > 800 AND o.shipdate > 800"),
]


# ------------------------------------------------------------------ data

def order_tables():
    """The order database: [(table name, columns, rows)], the same rows on every call.

    Everything comes from one generator with a fixed seed, and the tables are made in a fixed
    order, so nothing here may be reordered without changing the data. The integration workload
    (workload/) writes these same rows to CSV files and loads them through the prompt."""
    rng = random.Random(42)
    I, V = (lambda n: (n, 'INT', None)), (lambda n, size: (n, 'VARCHAR', size))
    out = []
    customers = []
    for cid in range(300):
        city = rng.randrange(30)
        customers.append((cid, f'cust{cid:03d}', CITIES[city], COUNTRIES[city // 5], rng.randrange(5),
                          'c' * rng.randrange(40, 61)))
    out.append(('Customer', [I('cid'), V('name', 16), V('city', 12), V('country', 10), I('segment'), V('note', 60)],
                customers))
    orders = []
    for oid in range(2000):
        odate = rng.randrange(1, 1001)
        status = 0 if rng.random() < 0.6 else rng.randrange(1, 5)       # most orders are status 0
        orders.append((oid, rng.randrange(300), odate, odate + rng.randrange(1, 6), status, rng.randrange(1, 5001),
                       'o' * rng.randrange(40, 61)))
    out.append(('Orders', [I('oid'), I('cid'), I('odate'), I('shipdate'), I('status'), I('total'), V('note', 60)],
                orders))
    weights = [1 / (p + 1) for p in range(200)]                         # a few products sell far more
    items = [(rng.randrange(2000), n % 5, rng.choices(range(200), weights)[0], rng.randrange(1, 11),
              rng.randrange(1, 500), 'i' * rng.randrange(20, 41)) for n in range(3000)]
    out.append(('Item', [I('oid'), I('lineno'), I('pid'), I('qty'), I('price'), V('note', 40)], items))
    products = [(pid, f'product{pid:03d}', (pid % 20) // 4, pid % 20, rng.randrange(1, 300), 'p' * rng.randrange(40, 61))
                for pid in range(200)]
    out.append(('Product', [I('pid'), V('pname', 20), I('category'), I('brand'), I('weight'), V('note', 60)], products))
    out.append(('Supplier', [I('sid'), V('sname', 16), V('country', 10)],
                [(sid, f'supplier{sid:02d}', COUNTRIES[sid % 6]) for sid in range(30)]))
    out.append(('Supply', [I('pid'), I('sid'), I('cost')],
                [(rng.randrange(200), rng.randrange(30), rng.randrange(1, 100)) for _ in range(500)]))
    return out


def build_database():
    if is_database(DB):
        return
    print('building', DB, flush=True)
    db = Database(DB, frames=64)
    for name, schema, rows in order_tables():
        heap = db.create_table(name, schema)
        for row in rows:
            heap.insert(row)
    db.close()
    db = Database(DB, frames=32)
    for name in db.catalog.list_tables():
        analyze(db, name)
    db.close()


# ------------------------------------------------------------------ running things

class Bench:

    def __init__(self, directory=DB):
        self.directory = directory
        self.sql_catalog = load_catalog(directory)
        self.cache = {}

    def logical(self, sql):
        return compile_sql(sql, self.sql_catalog)[0]

    def optimize(self, sql, frames=FRAMES, disabled=(), assume=None):
        db = Database(self.directory, 8)
        try:
            return Planner(db.catalog, frames, disabled, assume).optimize(self.logical(sql))
        finally:
            db.disk.close()

    def execute(self, physical, frames=FRAMES):
        """-> (row count, total page I/O, the Execution). The same plan at the same B is run once."""
        key = (json.dumps(physical, sort_keys=True), frames)
        if key not in self.cache:
            db = Database(self.directory, frames)
            try:
                execution = Execution(db, json.loads(key[0]))
                start = time.perf_counter()
                count = sum(1 for _ in execution.rows())
                totals = execution.totals()
                self.cache[key] = (count, totals['page_reads'] + totals['page_writes'], execution,
                                   time.perf_counter() - start)
            finally:
                db.disk.close()
        return self.cache[key][:3]


class Tables:
    def __init__(self):
        self.parts = []

    def add(self, title, header, rows, name):
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, name + '.csv'), 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        lines = [f'### {title}', '', '| ' + ' | '.join(header) + ' |', '|' + '---|' * len(header)]
        lines += ['| ' + ' | '.join(str(v) for v in row) + ' |' for row in rows]
        self.parts.append('\n'.join(lines))
        print('\n'.join(lines), '\n', flush=True)

    def text(self, name, content):
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, name), 'w', encoding='utf8') as f:
            f.write(content + '\n')
        print(content, '\n', flush=True)

    def save(self):
        with open(os.path.join(OUT, 'tables.md'), 'w', encoding='utf8') as f:
            f.write('\n\n'.join(self.parts) + '\n')


def q_error(estimated, actual):
    estimated, actual = max(estimated, 1), max(actual, 1)
    return max(estimated, actual) / min(estimated, actual)


# ------------------------------------------------------------------ 1. estimation quality

def study_quality(bench, tables):
    per_query, nodes = [], []
    for name, sql in QUERIES:
        result = bench.optimize(sql)
        count, io, execution = bench.execute(result.plan.to_json())
        errors = []
        for (_, node), (_, op) in zip(result.plan.walk(), execution.operators()):
            assert node.op == op.name, (node.op, op.name)
            q = q_error(node.rows, op.c.tuples_out)
            errors.append(q)
            nodes.append((q, name, node.label(), node.rows, op.c.tuples_out))
        per_query.append([name, len(errors), f'{statistics.median(errors):.2f}', f'{max(errors):.2f}',
                          result.plan.rows, count, round(result.plan.cost), io])
    tables.add('Estimated against actual, per query (B = %d)' % FRAMES,
               ['query', 'nodes', 'median q-error', 'worst q-error', 'est rows at root', 'actual rows at root',
                'est cost', 'actual I/O'], per_query, 'quality')
    all_q = [n[0] for n in nodes]
    summary = [[len(all_q), f'{statistics.median(all_q):.2f}', f'{statistics.mean(all_q):.2f}',
                f'{sorted(all_q)[int(len(all_q) * 0.9)]:.2f}', f'{max(all_q):.2f}',
                sum(1 for q in all_q if q <= 1.5), sum(1 for q in all_q if q > 10)]]
    tables.add('q-error over every node of every plan', ['nodes', 'median', 'mean', '90th percentile', 'worst',
                                                          'nodes within 1.5', 'nodes above 10'], summary, 'quality_summary')
    worst = sorted(nodes, reverse=True)[:12]
    tables.add('The twelve worst estimates', ['q-error', 'query', 'node', 'estimated rows', 'actual rows'],
               [[f'{q:.1f}', name, label if len(label) < 70 else label[:67] + '...', e, a]
                for q, name, label, e, a in worst], 'quality_worst')


# ------------------------------------------------------------------ 2. does optimization pay

def study_pays(bench, tables):
    rows, ratios = [], []
    for name, sql in QUERIES:
        best = bench.optimize(sql).plan.to_json()
        count, io, _ = bench.execute(best)
        plain_count, plain_io, plain = bench.execute(naive(bench.logical(sql)))
        assert count == plain_count, name
        ratio = plain_io / max(io, 1)
        ratios.append(ratio)
        rows.append([name, count, plain_io, io, f'{ratio:.2f}', plain.totals()['comparisons'],
                     bench.cache[(json.dumps(best, sort_keys=True), FRAMES)][2].totals()['comparisons']])
    rows.append(['all', '', sum(r[2] for r in rows), sum(r[3] for r in rows),
                 f'{sum(r[2] for r in rows) / sum(r[3] for r in rows):.2f}', sum(r[5] for r in rows),
                 sum(r[6] for r in rows)])
    tables.add('Page I/O with and without the optimizer (B = %d)' % FRAMES,
               ['query', 'rows', 'I/O unoptimized', 'I/O optimized', 'ratio', 'comparisons unoptimized',
                'comparisons optimized'], rows, 'pays')
    tables.add('Range of the ratio', ['smallest', 'median', 'largest'],
               [[f'{min(ratios):.2f}', f'{statistics.median(ratios):.2f}', f'{max(ratios):.2f}']], 'pays_range')
    changes = []
    for name, sql in QUERIES:                       # which join algorithm at three budgets
        picks = []
        for frames in (4, 8, 64):
            plan = bench.optimize(sql, frames).plan
            picks.append(' + '.join(n.op.replace('_join', '') for _, n in plan.walk() if n.op.endswith('_join')) or '-')
        if len(set(picks)) > 1:
            changes.append([name] + picks)
    tables.add('Queries whose join algorithms change with B', ['query', 'B = 4', 'B = 8', 'B = 64'], changes, 'pays_b')


# ------------------------------------------------------------------ 3. which rule mattered

RULES = {1: 'split conjunctions', 2: 'push selects below joins', 3: 'cross + select = join',
         4: 'push selects through rename, project, union', 5: 'push selects below group_by',
         6: 'push projections down', 7: 'tidy up'}


def study_rules(bench, tables):
    def workload(disabled):
        total, work, per_query = 0, 0, {}
        for name, sql in QUERIES:
            _, io, execution = bench.execute(bench.optimize(sql, disabled=disabled).plan.to_json())
            per_query[name] = io
            total += io
            work += execution.totals()['comparisons']
        return total, work, per_query

    full, full_work, base = workload(())
    rows = []
    for rule, what in RULES.items():
        total, work, per_query = workload({rule})
        changed = sorted(((per_query[q] - base[q], q) for q in base if per_query[q] != base[q]), reverse=True)
        rows.append([rule, what, full, total, f'{total / full:.2f}', len(changed),
                     ', '.join(f'{q} {d:+d}' for d, q in changed[:4]) or '-', f'{work / full_work:.2f}'])
    tables.add('Total page I/O of the workload with one rule turned off (B = %d)' % FRAMES,
               ['rule', 'what it does', 'I/O with all rules', 'I/O without this rule', 'ratio',
                'queries whose I/O changed', 'largest changes', 'comparisons, ratio'], rows, 'rules')


# ------------------------------------------------------------------ 4. where it breaks

def study_breaks(bench, tables):
    candidates = []
    for threshold in range(500, 951, 50):
        candidates.append((f'odate > {threshold} AND shipdate > {threshold}', 'o',
                           "SELECT o.oid, o.note, i.pid, i.note FROM Orders o JOIN Item i ON o.oid = i.oid "
                           f"WHERE o.odate > {threshold} AND o.shipdate > {threshold}"))
        candidates.append((f'odate > {threshold} AND shipdate > {threshold}, three tables', 'o',
                           "SELECT o.oid, o.note, i.note, p.pname FROM Orders o JOIN Item i ON o.oid = i.oid "
                           f"JOIN Product p ON i.pid = p.pid WHERE o.odate > {threshold} AND o.shipdate > {threshold}"))
        candidates.append((f'odate > {threshold} AND shipdate > {threshold}, four tables', 'o',
                           "SELECT o.note, i.note, p.pname, y.cost FROM Orders o JOIN Item i ON o.oid = i.oid "
                           "JOIN Product p ON i.pid = p.pid JOIN Supply y ON y.pid = p.pid "
                           f"WHERE o.odate > {threshold} AND o.shipdate > {threshold}"))
    for brand in range(8):                          # a brand belongs to exactly one category
        candidates.append((f'brand = {brand} AND category = {brand // 4}', 'p',
                           "SELECT p.pname, i.note, o.note FROM Product p JOIN Item i ON i.pid = p.pid "
                           f"JOIN Orders o ON o.oid = i.oid WHERE p.brand = {brand} AND p.category = {brand // 4}"))
    for city, country in (('Lima', 'Peru'), ('Tokyo', 'Japan'), ('Oslo', 'Norway')):
        candidates.append((f"city = '{city}' AND country = '{country}'", 'c',
                           "SELECT c.name, c.note, o.oid, o.note FROM Customer c JOIN Orders o ON c.cid = o.cid "
                           f"WHERE c.city = '{city}' AND c.country = '{country}'"))
    found = []
    for what, leaf, sql in candidates:
        for frames in (4, 5, 6, 7, 8, 10, 12, 16):
            guess = bench.optimize(sql, frames)
            _, io, execution = bench.execute(guess.plan.to_json(), frames)
            estimated = actual = None
            for (_, node), (_, op) in zip(guess.plan.walk(), execution.operators()):
                if node.op == 'filter' and what.split(' AND ')[0].split(' ')[0] in node.fields['condition']:
                    estimated, actual = node.rows, op.c.tuples_out
            informed = bench.optimize(sql, frames, assume={'rows': {leaf: actual}})
            if informed.plan.to_json() == guess.plan.to_json():
                continue
            _, better_io, _ = bench.execute(informed.plan.to_json(), frames)
            found.append((io / better_io, what, frames, estimated, actual, io, better_io, sql, guess, informed))
    found.sort(key=lambda f: -f[0])
    rows = [[what, frames, est, act, f'{act / est:.1f}', io, better, f'{ratio:.2f}']
            for ratio, what, frames, est, act, io, better, *_ in found[:12]]
    tables.add('Correlated predicates: the plan chosen against the plan chosen when told the true row count',
               ['predicates', 'B', 'estimated rows', 'actual rows', 'actual / estimated', 'I/O of the chosen plan',
                'I/O of the informed plan', 'ratio'], rows, 'breaks')
    if found:
        ratio, what, frames, est, act, io, better, sql, guess, informed = found[0]
        text = [f'query: {sql}', f'B = {frames}', '',
                f'rows passing the two predicates: estimated {est}, actual {act}', '',
                f'chosen plan, {io} page I/O:', guess.explain(own=True), '',
                f'plan chosen when told there are {act} rows, {better} page I/O:', informed.explain(own=True), '']
        for label, result in (('chosen', guess), ('informed', informed)):
            _, _, execution = bench.execute(result.plan.to_json(), frames)
            text += [f'what the {label} plan did:'] + execution.trace(own=True) + ['']
        tables.text('breaks_case.txt', '\n'.join(text))
    single = []
    for sql, parts in [("SELECT oid FROM Orders WHERE odate > 800 AND shipdate > 800",
                        ("odate > 800", "shipdate > 800")),
                       ("SELECT cid FROM Customer WHERE city = 'Lima' AND country = 'Peru'",
                        ("city = 'Lima'", "country = 'Peru'")),
                       ("SELECT oid FROM Orders WHERE odate < 100 AND shipdate > 900",
                        ("odate < 100", "shipdate > 900"))]:
        table_name = sql.split(' FROM ')[1].split(' ')[0]
        counts = []
        for condition in parts + (' AND '.join(parts),):
            q = f"SELECT COUNT(*) FROM {table_name} WHERE {condition}"
            db = Database(bench.directory, 8)
            execution = Execution(db, bench.optimize(q).plan.to_json())
            counts.append(next(iter(execution.rows()))[0])
            db.disk.close()
        estimated = [n.rows for _, n in bench.optimize(sql).plan.walk() if n.op == 'filter'][0]
        total = bench.sql_catalog and counts
        single.append([' AND '.join(parts), counts[0], counts[1], estimated, counts[2],
                       f'{q_error(estimated, counts[2]):.1f}'])
    tables.add('The independence assumption on the two correlated pairs',
               ['predicates', 'rows passing the first', 'rows passing the second', 'estimated rows passing both',
                'actual rows passing both', 'q-error'], single, 'breaks_independence')


# ------------------------------------------------------------------ the DP table of a four-relation query

def study_dp(bench, tables):
    sql = dict(QUERIES)['Q12']
    result = bench.optimize(sql)
    tables.text('dp_table.txt', f'{sql}\nB = {FRAMES}\n\n' + result.dp_text() + '\n\nchosen plan\n' + result.explain(own=True))
    tables.text('rewrite_example.txt', bench.optimize(dict(QUERIES)['Q5']).rewrite_text())


STUDIES = {'quality': study_quality, 'pays': study_pays, 'rules': study_rules, 'breaks': study_breaks, 'dp': study_dp}


def main():
    build_database()
    wanted = sys.argv[1:] or list(STUDIES)
    bench, tables = Bench(), Tables()
    for name in wanted:
        start = time.perf_counter()
        STUDIES[name](bench, tables)
        print(f'[{name}: {time.perf_counter() - start:.0f} s]\n', flush=True)
    if len(wanted) == len(STUDIES):
        tables.save()


if __name__ == '__main__':
    main()
