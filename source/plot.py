"""Draw the log-log chart for REPORT.md as an SVG file, without a plotting library.

    python -m source.plot     reads results/join.csv and results/unary.csv,
                              writes results/loglog.svg and prints the fitted slopes

The slope of log10(time) against log10(n) is a least-squares fit; slope 2 means doubling n
quadruples the time, slope 1 means doubling n doubles the time.
"""
import csv
import math
import os
import sys

SERIES = [  # label, csv file, time column, colour
    ('join', 'join.csv', 'wall_time_s', '#2a78d6'),
    ('select', 'unary.csv', 'select_time_s', '#eb6834'),
    ('project', 'unary.csv', 'project_time_s', '#1baf7a'),
]
INK, INK2, GRID, SURFACE = '#0b0b0b', '#52514e', '#e4e3df', '#fcfcfb'


def read(path, ycol):
    with open(path, newline='') as f:
        return [(float(r['n']), float(r[ycol])) for r in csv.DictReader(f)]


def fit(points):
    """Least-squares line through (log10 n, log10 t). Returns (slope, intercept)."""
    xs = [math.log10(x) for x, _ in points]
    ys = [math.log10(y) for _, y in points]
    k = len(xs)
    mx, my = sum(xs) / k, sum(ys) / k
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, my - slope * mx


def decade_label(d):
    return str(10 ** d) if d >= 0 else '0.' + '0' * (-d - 1) + '1'


def draw(series, out):
    W, H = 760, 460
    L, R, T, B = 80, 150, 50, 70
    pw, ph = W - L - R, H - T - B
    xs = [x for _, pts, _, _ in series for x, _ in pts]
    ys = [y for _, pts, _, _ in series for _, y in pts]
    x0, x1 = math.log10(min(xs)), math.log10(max(xs))
    y0, y1 = math.floor(math.log10(min(ys))), math.ceil(math.log10(max(ys)))

    def X(v):
        return L + (math.log10(v) - x0) / (x1 - x0) * pw

    def Y(v):
        return T + ph - (math.log10(v) - y0) / (y1 - y0) * ph

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         f'font-family="system-ui, sans-serif" font-size="12">',
         '<title>Wall time against n on log-log axes</title>',
         f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>',
         f'<text x="{L}" y="24" font-size="15" font-weight="600" fill="{INK}">'
         f'Wall time against n on log-log axes (n = m)</text>']
    for d in range(y0, y1 + 1):                        # one grid line per decade
        y = Y(10 ** d)
        o.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L + pw}" y2="{y:.1f}" stroke="{GRID}"/>')
        o.append(f'<text x="{L - 8}" y="{y + 4:.1f}" text-anchor="end" fill="{INK2}">{decade_label(d)}</text>')
    for n in sorted(set(xs)):                          # x ticks at the measured sizes
        x = X(n)
        o.append(f'<line x1="{x:.1f}" y1="{T + ph}" x2="{x:.1f}" y2="{T + ph + 5}" stroke="{INK2}"/>')
        o.append(f'<text x="{x:.1f}" y="{T + ph + 20}" text-anchor="middle" fill="{INK2}">{int(n)}</text>')
    o.append(f'<line x1="{L}" y1="{T + ph}" x2="{L + pw}" y2="{T + ph}" stroke="{INK2}"/>')
    o.append(f'<text x="{L + pw / 2:.1f}" y="{H - 22}" text-anchor="middle" fill="{INK}">'
             f'n = tuples per relation (log scale)</text>')
    o.append(f'<text x="18" y="{T + ph / 2:.1f}" text-anchor="middle" fill="{INK}" '
             f'transform="rotate(-90 18 {T + ph / 2:.1f})">wall time in seconds (log scale)</text>')
    for i, (label, pts, colour, _) in enumerate(series):   # legend, top left of the plot area
        y = T + 14 + i * 18
        o.append(f'<line x1="{L + 12}" y1="{y}" x2="{L + 34}" y2="{y}" stroke="{colour}" stroke-width="2"/>')
        o.append(f'<text x="{L + 40}" y="{y + 4}" fill="{INK}">{label}</text>')
    label_ys = []
    for label, pts, colour, (slope, _) in series:
        path = ' '.join(f'{X(x):.1f},{Y(y):.1f}' for x, y in pts)
        o.append(f'<polyline points="{path}" fill="none" stroke="{colour}" stroke-width="2"/>')
        for x, y in pts:
            o.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="4" fill="{colour}" '
                     f'stroke="{SURFACE}" stroke-width="2"/>')
        lx, ly = pts[-1]
        ty = Y(ly) + 4
        while any(abs(ty - other) < 14 for other in label_ys):   # keep end labels from overlapping
            ty += 14
        label_ys.append(ty)
        o.append(f'<text x="{X(lx) + 10:.1f}" y="{ty:.1f}" fill="{INK}">{label} (slope {slope:.2f})</text>')
    o.append('</svg>')
    with open(out, 'w', encoding='utf8') as f:
        f.write('\n'.join(o))


def main(results='results'):
    series = []
    for label, file, ycol, colour in SERIES:
        pts = read(os.path.join(results, file), ycol)
        line = fit(pts)
        series.append((label, pts, colour, line))
        print(f'{label:8s} slope of log10(time) against log10(n): {line[0]:.3f}')
    draw(series, os.path.join(results, 'loglog.svg'))
    print('wrote', os.path.join(results, 'loglog.svg'))


if __name__ == '__main__':
    main(*sys.argv[1:])
