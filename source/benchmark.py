"""Driver for the performance study in REPORT.md. Every number in that report comes from the CSV
files this script writes into results/.

    python -m source.benchmark unary      select and project on R at n = 1000 .. 64000  -> results/unary.csv
    python -m source.benchmark matchrate  the join at one size and several match rates  -> results/match_rate.csv
    python -m source.benchmark join       R join[R.b=S.b] S at n = m = 1000 .. 64000     -> results/join.csv
    python -m source.benchmark all        the three above, in that order

Only evaluate() is timed. Generating and loading the relation files is not part of the measurement.
The comparison counts are read from the operators' own counters, never computed from n and m.
"""
import argparse
import csv
import os
import tempfile
import time
from .generate_data import generate
from .relation_file import load_relations
from .tokenizer import Tokenizer
from .parser import Parser
from .engine import evaluate
from .operators import Counters

SIZES = [1000, 2000, 4000, 8000, 16000, 32000, 64000]
JOIN_QUERY = 'R join[R.b=S.b] S'
SELECT_QUERY = 'select[b<1000](R)'
PROJECT_QUERY = 'project[b](R)'


def load(workdir):
    db = {}
    for name in ('R.ra', 'S.ra'):
        db.update(load_relations(os.path.join(workdir, name)))
    return db


def timed(query, db, repeat=1):
    """Run the query `repeat` times. Returns (best wall time, counters of one run, output size)."""
    ast = Parser(Tokenizer(query).scan()).parse()
    best = None
    for _ in range(repeat):
        counters = Counters()
        start = time.perf_counter()
        result = evaluate(ast, db, counters)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    return best, counters, len(result.rows)


def write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def run_join(sizes, match_rate, workdir, out):
    rows = []
    for n in sizes:
        generate(n, n, match_rate, workdir)
        db = load(workdir)
        secs, c, produced = timed(JOIN_QUERY, db)
        rows.append([n, n, c.join_compared, f'{secs:.4f}', produced])
        print(f'join   n=m={n:>6}: {c.join_compared:>12} comparisons {secs:10.3f} s '
              f'{produced:>6} output tuples', flush=True)
        write_csv(out, ['n', 'm', 'comparisons', 'wall_time_s', 'output_tuples'], rows)  # saved after every size


def run_unary(sizes, match_rate, workdir, out, repeat):
    rows = []
    for n in sizes:
        generate(n, n, match_rate, workdir)
        db = load(workdir)
        s_secs, sc, s_out = timed(SELECT_QUERY, db, repeat)
        p_secs, _, p_out = timed(PROJECT_QUERY, db, repeat)
        rows.append([n, sc.select_examined, f'{s_secs:.6f}', s_out, f'{p_secs:.6f}', p_out])
        print(f'unary  n={n:>6}: select examined {sc.select_examined:>6} tuples in {s_secs:.6f} s '
              f'({s_out} out); project {p_secs:.6f} s ({p_out} out)', flush=True)
    write_csv(out, ['n', 'select_examined', 'select_time_s', 'select_output',
                    'project_time_s', 'project_output'], rows)


def run_matchrate(n, rates, workdir, out):
    rows = []
    for k in rates:
        generate(n, n, k, workdir)
        db = load(workdir)
        secs, c, produced = timed(JOIN_QUERY, db)
        rows.append([n, n, k, c.join_compared, f'{secs:.4f}', produced])
        print(f'match  n=m={n} rate={k}: {c.join_compared} comparisons {secs:.3f} s '
              f'{produced} output tuples', flush=True)
    write_csv(out, ['n', 'm', 'match_rate', 'comparisons', 'wall_time_s', 'output_tuples'], rows)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('experiment', choices=['join', 'unary', 'matchrate', 'all'])
    p.add_argument('--sizes', default=','.join(map(str, SIZES)))
    p.add_argument('--match-rate', type=float, default=0.1)
    p.add_argument('--rates', default='0,0.1,0.5,1.0', help='match rates for the matchrate experiment')
    p.add_argument('--matchrate-n', type=int, default=8000, help='n = m for the matchrate experiment')
    p.add_argument('--repeat', type=int, default=5, help='repeats for the unary experiment; best time kept')
    p.add_argument('--workdir', default=os.path.join(tempfile.gettempdir(), 'ra_bench'))
    p.add_argument('--results', default='results')
    a = p.parse_args()
    sizes = [int(s) for s in a.sizes.split(',')]
    if a.experiment in ('unary', 'all'):
        run_unary(sizes, a.match_rate, a.workdir, os.path.join(a.results, 'unary.csv'), a.repeat)
    if a.experiment in ('matchrate', 'all'):
        rates = [float(r) for r in a.rates.split(',')]
        run_matchrate(a.matchrate_n, rates, a.workdir, os.path.join(a.results, 'match_rate.csv'))
    if a.experiment in ('join', 'all'):
        run_join(sizes, a.match_rate, a.workdir, os.path.join(a.results, 'join.csv'))


if __name__ == '__main__':
    main()
