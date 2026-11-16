"""Data generator for the performance study. Writes R(a, b) and S(b, c) as relation files.

    python -m source.generate_data --n 1000 --m 1000 --match-rate 0.1 --out data/generated

R has n tuples (i, i) for i = 0 .. n-1, so R.b is unique.
S has m tuples. --match-rate k is the average number of S tuples each R tuple joins with on b:
round(k * n) of the S tuples get a b that exists in R, spread evenly over R's b values; every
other S tuple gets a b of n + j, which matches nothing. So with n = m and k = 0.1 the join
R join[R.b=S.b] S produces 0.1 * n tuples, with k = 1 it produces n, and with k = 0 none.
--seed only shuffles the order of S's tuples; a nested loop does not care about order.
"""
import argparse
import os
import random


def generate(n, m, match_rate, out, seed=1):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'R.ra'), 'w') as f:
        f.write('R (a, b) = {\n')
        for i in range(n):
            f.write(f'  {i}, {i}\n')
        f.write('}\n')
    matching = min(m, round(match_rate * n))
    s_rows = []
    for j in range(m):
        b = j % n if j < matching else n + j
        s_rows.append((b, j))
    random.Random(seed).shuffle(s_rows)
    with open(os.path.join(out, 'S.ra'), 'w') as f:
        f.write('S (b, c) = {\n')
        for b, c in s_rows:
            f.write(f'  {b}, {c}\n')
        f.write('}\n')
    return matching


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--n', type=int, required=True, help='tuples in R')
    p.add_argument('--m', type=int, required=True, help='tuples in S')
    p.add_argument('--match-rate', type=float, default=0.1,
                   help='average number of S tuples each R tuple joins with (default 0.1)')
    p.add_argument('--out', required=True, help='directory for R.ra and S.ra')
    p.add_argument('--seed', type=int, default=1)
    a = p.parse_args()
    if a.n < 1 or a.m < 0:
        p.error('--n must be at least 1 and --m at least 0')
    matching = generate(a.n, a.m, a.match_rate, a.out, a.seed)
    print(f'wrote R with {a.n} tuples and S with {a.m} tuples to {a.out}; '
          f'{matching} S tuples have a partner in R')


if __name__ == '__main__':
    main()
