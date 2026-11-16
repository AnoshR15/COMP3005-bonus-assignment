"""The measurements of REPORT_M2.md. Every number in that report comes from the files this writes.

    python -m bench.m2_measure            all five studies
    python -m bench.m2_measure cost sort  only the named ones: cost sort crossover skew budget

Writes results/m2/*.csv, two SVG charts and results/m2/tables.md (the same tables in Markdown).
The database is built once in bench/m2db and reused.

The tables hold wide rows, (k INT, v INT, pad VARCHAR(200)), 19 to a page, so that a table of
many pages still has few enough rows for a nested loop join in Python to finish. k is a
permutation of 0..rows-1, so A_m join B_n on k matches every row of the smaller table once.
"""
import csv
import math
import os
import sys
import time
from executor.run import Execution
from store.db import Database, is_database

DB = os.path.join('bench', 'm2db')
OUT = os.path.join('results', 'm2')
COLUMNS = [('k', 'INT', None), ('v', 'INT', None), ('pad', 'VARCHAR', 200)]
ROWS_PER_PAGE = 19
A_PAGES = [8, 10, 20, 30, 40, 60, 80, 120, 160, 240, 320]
B_PAGES = [10, 20, 40, 60, 80, 160, 240]
SKEW_PAGES = 60
JOINS = ['nested_loop_join', 'sort_merge_join', 'hash_join']
SHORT = {'nested_loop_join': 'block nested loop', 'sort_merge_join': 'sort-merge', 'hash_join': 'hash'}


# ------------------------------------------------------------------ data

def build_database():
    if is_database(DB):
        return
    print('building', DB)
    db = Database(DB, frames=64)

    def table(name, keys):
        heap = db.create_table(name, COLUMNS)
        for i, k in enumerate(keys):
            heap.insert((k, i, f'{i:06d}' + 'p' * 194))
        assert heap.info.num_pages == math.ceil(len(keys) / ROWS_PER_PAGE), (name, heap.info.num_pages)

    def permutation(n, step):
        return [(i * step + 7) % n for i in range(n)]       # step is odd and not a multiple of 19

    for pages in A_PAGES:
        rows = pages * ROWS_PER_PAGE
        table(f'A{pages}', permutation(rows, 7919 if rows % 7919 else 7907))
    for pages in B_PAGES:
        rows = pages * ROWS_PER_PAGE
        table(f'B{pages}', permutation(rows, 104729))
    # Skew: key 0 in 40 percent of the rows on both sides, every other key once.
    rows = SKEW_PAGES * ROWS_PER_PAGE
    hot = [0 if (i * 7) % 5 < 2 else i for i in range(rows)]
    table('SkewR', hot)
    table('SkewS', [hot[(i * 31) % rows] for i in range(rows)])
    db.close()


def scan(table):
    return {'op': 'seqscan', 'table': table}


def join(kind, left, right):
    return {'op': kind, 'condition': f'{left}.k = {right}.k', 'left': scan(left), 'right': scan(right)}


def measure(plan, frames):
    """Run a plan, throw the rows away, return the report and the wall time."""
    db = Database(DB, frames)
    try:
        execution = Execution(db, plan)
        start = time.perf_counter()
        for _ in execution.rows():
            pass
        report = execution.report()
        report['seconds'] = time.perf_counter() - start
        return report
    finally:
        db.disk.close()


def find(report_node, label):
    if report_node['op'] == label:
        return report_node
    for child in report_node['inputs']:
        hit = find(child, label)
        if hit:
            return hit
    return None


# ------------------------------------------------------------------ the formulas of the specification

def sort_passes(n, b):
    """1 + ceil(log(ceil(n / b)) / log(b - 1)), computed with integers."""
    runs, passes = math.ceil(n / b), 1
    while runs > 1:
        runs = math.ceil(runs / (b - 1))
        passes += 1
    return passes


def formula(kind, m, n, b):
    """(page reads, page writes) of a join by the textbook formula."""
    if kind == 'nested_loop_join':
        return m + math.ceil(m / (b - 2)) * n, 0
    if kind == 'sort_merge_join':                   # sort(M) + sort(N) + M + N, sort(X) = 2 X passes
        both = m * sort_passes(m, b) + n * sort_passes(n, b)
        return both + m + n, both
    return 2 * (m + n), m + n                       # 3 (M + N)


# ------------------------------------------------------------------ output helpers

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

    def save(self, name):
        with open(os.path.join(OUT, name), 'w', encoding='utf8') as f:
            f.write('\n\n'.join(self.parts) + '\n')


def chart(path, title, xlabel, ylabel, series, marks=()):
    """Lines on log-log axes, written as SVG by hand. series: [(label, colour, [(x, y)])]."""
    W, H, L, R, T, B = 760, 440, 78, 170, 46, 62
    pw, ph = W - L - R, H - T - B
    xs = [x for _, _, pts in series for x, _ in pts]
    ys = [y for _, _, pts in series for _, y in pts]
    x0, x1 = math.log10(min(xs)), math.log10(max(xs))
    y0, y1 = math.floor(math.log10(min(ys)) * 2) / 2, math.ceil(math.log10(max(ys)) * 2) / 2

    def X(v):
        return L + (math.log10(v) - x0) / (x1 - x0) * pw

    def Y(v):
        return T + ph - (math.log10(v) - y0) / (y1 - y0) * ph

    ink, ink2, grid = '#0b0b0b', '#52514e', '#e4e3df'
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         'font-family="system-ui, sans-serif" font-size="12">',
         f'<title>{title}</title>', f'<rect width="{W}" height="{H}" fill="#fcfcfb"/>',
         f'<text x="{L}" y="26" font-size="15" fill="{ink}">{title}</text>']
    for x in sorted(set(xs)):
        o.append(f'<line x1="{X(x):.1f}" y1="{T}" x2="{X(x):.1f}" y2="{T + ph}" stroke="{grid}"/>')
        o.append(f'<text x="{X(x):.1f}" y="{T + ph + 18}" text-anchor="middle" fill="{ink2}">{x}</text>')
    tick = y0
    while tick <= y1 + 1e-9:
        value = 10 ** tick
        if abs(tick - round(tick)) < 1e-9:
            o.append(f'<line x1="{L}" y1="{Y(value):.1f}" x2="{L + pw}" y2="{Y(value):.1f}" stroke="{grid}"/>')
            o.append(f'<text x="{L - 8}" y="{Y(value) + 4:.1f}" text-anchor="end" fill="{ink2}">{int(round(value)):,}</text>')
        tick += 0.5
    o.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="{ink2}"/>')
    for x, text in marks:
        o.append(f'<line x1="{X(x):.1f}" y1="{T}" x2="{X(x):.1f}" y2="{T + ph}" stroke="{ink2}" stroke-dasharray="4 3"/>')
        o.append(f'<text x="{X(x) + 4:.1f}" y="{T + 14}" fill="{ink2}">{text}</text>')
    for i, (label, colour, pts) in enumerate(series):
        path_d = ' '.join(f'{"M" if j == 0 else "L"}{X(x):.1f},{Y(y):.1f}' for j, (x, y) in enumerate(pts))
        o.append(f'<path d="{path_d}" fill="none" stroke="{colour}" stroke-width="2"/>')
        o += [f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="3" fill="{colour}"/>' for x, y in pts]
        ly = T + 16 + 22 * i
        o.append(f'<line x1="{L + pw + 16}" y1="{ly}" x2="{L + pw + 40}" y2="{ly}" stroke="{colour}" stroke-width="2"/>')
        o.append(f'<text x="{L + pw + 46}" y="{ly + 4}" fill="{ink}">{label}</text>')
    o.append(f'<text x="{L + pw / 2}" y="{H - 16}" text-anchor="middle" fill="{ink}">{xlabel}</text>')
    o.append(f'<text x="18" y="{T + ph / 2}" text-anchor="middle" fill="{ink}" '
             f'transform="rotate(-90 18 {T + ph / 2})">{ylabel}</text>')
    o.append('</svg>')
    with open(path, 'w', encoding='utf8') as f:
        f.write('\n'.join(o) + '\n')


COLOURS = {'nested_loop_join': '#2a78d6', 'sort_merge_join': '#eb6834', 'hash_join': '#1baf7a'}


# ------------------------------------------------------------------ 1. cost against formula

def study_cost(tables):
    rows = []
    for m, n in [(20, 10), (60, 40), (120, 80)]:
        for b in (4, 16, 64, 256):
            for kind in JOINS:
                r = measure(join(kind, f'A{m}', f'B{n}'), b)
                t = r['totals']
                fr, fw = formula(kind, m, n, b)
                rows.append([SHORT[kind], m, n, b, fr, t['page_reads'], fw, t['page_writes'],
                             fr + fw, t['page_reads'] + t['page_writes'], t['comparisons'], r['peak_frames']])
    tables.add('Cost against formula', ['join', 'M', 'N', 'B', 'reads: formula', 'reads: measured',
                                        'writes: formula', 'writes: measured', 'I/O: formula',
                                        'I/O: measured', 'comparisons', 'peak frames'], rows, 'cost')


# ------------------------------------------------------------------ 2. sort passes

def study_sort(tables):
    rows = []
    for n, b in [(8, 3), (8, 4), (30, 4), (80, 4), (120, 8), (160, 8), (120, 16), (320, 16), (60, 64), (320, 64)]:
        r = measure({'op': 'sort', 'keys': [[f'A{n}.k', 'asc']], 'child': scan(f'A{n}')}, b)
        d, t = r['plan']['details'], r['totals']
        expected = sort_passes(n, b)
        rows.append([n, b, '20 x B' if n == 20 * b else '', math.ceil(n / b), d['initial_runs'], expected,
                     d['passes'], n * expected, t['page_reads'], n * (expected - 1), t['page_writes']])
    tables.add('Sort passes', ['N pages', 'B', '', 'runs: formula', 'runs: measured', 'passes: formula',
                               'passes: measured', 'reads: N x passes', 'reads: measured',
                               'writes: N x (passes - 1)', 'writes: measured'], rows, 'sort_passes')


# ------------------------------------------------------------------ 3. crossover

def study_crossover(tables):
    m, n = 120, 80
    budgets = [4, 5, 6, 8, 10, 12, 16, 24, 32, 48, 64, 96, 122, 128, 192, 256]
    rows, series = [], {kind: [] for kind in JOINS}
    for b in budgets:
        cost = {}
        for kind in JOINS:
            t = measure(join(kind, f'A{m}', f'B{n}'), b)['totals']
            cost[kind] = t['page_reads'] + t['page_writes']
            series[kind].append((b, cost[kind]))
        rows.append([b] + [cost[k] for k in JOINS] + [SHORT[min(JOINS, key=lambda k: cost[k])]])
    tables.add(f'Crossover, relations fixed (M = {m}, N = {n}), B varies',
               ['B'] + [SHORT[k] for k in JOINS] + ['cheapest'], rows, 'crossover_b')
    chart(os.path.join(OUT, 'crossover_b.svg'), f'Page I/O of three joins, M = {m} and N = {n} pages',
          'B, frames (log scale)', 'page reads + writes (log scale)',
          [(SHORT[k], COLOURS[k], series[k]) for k in JOINS])

    b = 16
    rows, series = [], {kind: [] for kind in JOINS}
    for size in (10, 20, 40, 80, 160, 240):
        cost = {}
        for kind in JOINS:
            t = measure(join(kind, f'A{size}', f'B{size}'), b)['totals']
            cost[kind] = t['page_reads'] + t['page_writes']
            series[kind].append((size, cost[kind]))
        rows.append([size] + [cost[k] for k in JOINS] + [SHORT[min(JOINS, key=lambda k: cost[k])]])
    tables.add(f'Crossover, B fixed at {b}, relation size varies (M = N)',
               ['M = N pages'] + [SHORT[k] for k in JOINS] + ['cheapest'], rows, 'crossover_size')
    chart(os.path.join(OUT, 'crossover_size.svg'), f'Page I/O of three joins at B = {b}',
          'pages in each relation (log scale)', 'page reads + writes (log scale)',
          [(SHORT[k], COLOURS[k], series[k]) for k in JOINS])


# ------------------------------------------------------------------ 4. skew

def study_skew(tables):
    rows, parts = [], []
    for b in (4, 16, 64):
        for name, left, right in [('uniform', f'A{SKEW_PAGES}', f'B{SKEW_PAGES}'), ('skewed', 'SkewR', 'SkewS')]:
            r = measure(join('hash_join', left, right), b)
            d, t = r['plan']['details'], r['totals']
            actions = [o['action'] for o in d['overflows']]
            rows.append([name, b, d['partitions'], d['table_frames'], max(d['left_partition_pages']),
                         actions.count('partitioned again'), actions.count('nested loop'),
                         t['page_reads'], t['page_writes'], t['page_reads'] + t['page_writes'],
                         t['comparisons'], t['tuples_out'], f"{r['seconds']:.1f}"])
            if b == 16:
                parts.append([name, ' '.join(str(p) for p in d['left_partition_pages']),
                              ' '.join(str(p) for p in d['right_partition_pages'])])
                if name == 'skewed':
                    overflow_rows = [[o['level'], o['left_pages'], o['right_pages'], o['action']] for o in d['overflows']]
    tables.add(f'Hash join, uniform against skewed keys (M = N = {SKEW_PAGES} pages)',
               ['keys', 'B', 'partitions', 'table frames', 'largest left partition, pages', 'partitioned again',
                'nested loops', 'reads', 'writes', 'I/O', 'comparisons', 'rows out', 'seconds'], rows, 'skew')
    tables.add('Partition sizes in pages at B = 16', ['keys', 'left partitions', 'right partitions'], parts, 'skew_partitions')
    tables.add('What the overflow handling did, skewed keys, B = 16',
               ['level', 'left pages', 'right pages', 'action'], overflow_rows, 'skew_overflow')
    rows = []
    for kind in JOINS:
        r = measure(join(kind, 'SkewR', 'SkewS'), 16)
        t = r['totals']
        rows.append([SHORT[kind], t['page_reads'], t['page_writes'], t['comparisons'], t['tuples_out'], f"{r['seconds']:.1f}"])
    tables.add('The three joins on the skewed data at B = 16',
               ['join', 'reads', 'writes', 'comparisons', 'rows out', 'seconds'], rows, 'skew_joins')


# ------------------------------------------------------------------ 5. the budget

def study_budget(tables):
    rows = []
    plans = [
        ('sort of A80 (20 x B pages)', {'op': 'sort', 'keys': [['A80.k', 'asc']], 'child': scan('A80')}),
        ('sort over hash join over nested loop',
         {'op': 'sort', 'keys': [['A80.k', 'desc']],
          'child': {'op': 'hash_join', 'condition': 'A80.k = B40.k',
                    'left': {'op': 'nested_loop_join', 'condition': 'A80.k = A20.k',
                             'left': scan('A80'), 'right': scan('A20')},
                    'right': scan('B40')}}),
        ('distinct (hash) of a sort-merge join',
         {'op': 'distinct', 'mode': 'hash', 'child': join('sort_merge_join', 'A60', 'B60')}),
    ]
    for name, plan in plans:
        r = measure(plan, 4)
        t = r['totals']
        root = r['plan']['details']
        rows.append([name, 4, r['peak_frames'], t['tuples_out'], t['page_reads'], t['page_writes'],
                     root.get('initial_runs', ''), root.get('passes', '')])
    tables.add('Plans at B = 4', ['plan', 'B', 'most frames in use at once', 'rows out', 'reads', 'writes',
                                  'runs', 'passes'], rows, 'budget')


STUDIES = {'cost': study_cost, 'sort': study_sort, 'crossover': study_crossover, 'skew': study_skew,
           'budget': study_budget}


def main():
    build_database()
    wanted = sys.argv[1:] or list(STUDIES)
    tables = Tables()
    for name in wanted:
        start = time.perf_counter()
        STUDIES[name](tables)
        print(f'[{name}: {time.perf_counter() - start:.0f} s]\n', flush=True)
    tables.save('tables.md' if len(wanted) == len(STUDIES) else 'tables_' + '_'.join(wanted) + '.md')


if __name__ == '__main__':
    main()
