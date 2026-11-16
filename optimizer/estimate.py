"""Cardinality estimation: how many rows a predicate keeps, and what a node's output looks like.

An Est is the optimizer's picture of a stream of rows: a row count and, for every column, a
ColEst with the column's type, average width, number of distinct values, lowest and highest
value, and histogram. Estimates are pushed through the plan from the leaves up.

Selectivity of a comparison between a column c and a constant v:

    c = v      the bucket holding v:  tuples(bucket) / (tuples(all buckets) * ndv(bucket))
               no histogram: 1 / ndv(c);  v outside [min, max]: 0
    c < v      whole buckets below v, plus a linear fraction (v - low) / (high - low) of the
    c > v      bucket holding v (the mirror image for >); no histogram: the same fraction of
               [min, max]. Strings have no "linear": half of the bucket holding v.
    c <= v     sel(c < v) + sel(c = v), and the same for >=
    c <> v     1 - sel(c = v)

and of everything else:

    p AND q    sel(p) * sel(q)            independence
    p OR q     sel(p) + sel(q) - sel(p) * sel(q)
    NOT p      1 - sel(p)
    c1 = c2    1 / max(ndv(c1), ndv(c2))  two columns of the same input, and the join formula
    any other comparison of two columns   0.1

One improvement over multiplying blindly. After a filter on c, c is no longer distributed as it
was in the table: its histogram is cut down to the part the filter kept (restrict()). A second
predicate on the same column is then measured against what is left, so  a > 10 AND a < 20  is
estimated as the rows between 10 and 20, not as sel(a > 10) * sel(a < 20). Different columns
are still multiplied as if independent, which is the assumption REPORT_M4.md breaks on purpose.

With no statistics (a table never analyzed): the row and page counts still come from the
catalog; equality keeps 0.1, a range keeps 1/3, a join predicate keeps 0.1; an INT is 4 bytes
wide and a VARCHAR(n) is 2 + n/2.

A node's output cardinality is its input's times the selectivity, rounded up, never below 1.
"""
import math
from source.ast import Attr, Literal, Compare, Logic
from store import page
from store.record import INT, VARCHAR, REAL

DEFAULT_EQUAL = 0.1
DEFAULT_RANGE = 1 / 3
DEFAULT_OTHER = 0.1         # a comparison of two columns that is not an equality; and any join
                            # predicate when the number of distinct values is unknown
FLIP = {'<': '>', '<=': '>=', '>': '<', '>=': '<=', '=': '=', '<>': '<>', '!=': '!='}


def round_rows(rows):
    return max(1, math.ceil(rows - 1e-9))


def pages_for(rows, width):
    """Pages that `rows` rows of `width` bytes occupy: each row also costs a 4-byte slot, and a
    page has 4088 usable bytes. (The specification's default is ceil(rows * width / 4096); this
    is the same idea with the page format of Module 1 taken into account, so that a table's
    estimated size agrees with its real one.)"""
    return max(1, math.ceil(rows * (width + page.SLOT_SIZE) / page.CAPACITY))


class ColEst:
    __slots__ = ('kind', 'width', 'ndv', 'lo', 'hi', 'hist')

    def __init__(self, kind, width, ndv=None, lo=None, hi=None, hist=None):
        self.kind = kind
        self.width = width          # average bytes
        self.ndv = ndv              # None when unknown
        self.lo = lo
        self.hi = hi
        self.hist = hist            # list of [low, high, tuples, ndv], or None

    @staticmethod
    def default(kind, size):
        return ColEst(kind, 4 if kind == INT else 8 if kind == REAL else 2 + size / 2)

    @staticmethod
    def from_stats(kind, size, s):
        if not s or s.get('ndv') is None:
            return ColEst.default(kind, size)
        return ColEst(kind, s.get('width') or ColEst.default(kind, size).width, s['ndv'], s['min'], s['max'],
                      [list(b) for b in s['hist']] if s.get('hist') else None)

    def copy(self):
        return ColEst(self.kind, self.width, self.ndv, self.lo, self.hi, self.hist)

    def comparable(self, value):
        return (self.kind == VARCHAR) == isinstance(value, str)

    # ---- fractions of this column's rows

    def equal(self, v):
        if not self.comparable(v):
            return DEFAULT_EQUAL
        if self.hist:
            total = sum(b[2] for b in self.hist)
            for low, high, tuples, ndv in self.hist:
                if low <= v <= high:
                    return tuples / (total * ndv)
            return 0.0
        if self.ndv:
            if self.lo is not None and not self.lo <= v <= self.hi:
                return 0.0
            return 1.0 / self.ndv
        return DEFAULT_EQUAL

    def below(self, v):
        """Fraction with c < v."""
        return self._side(v, below=True)

    def above(self, v):
        """Fraction with c > v."""
        return self._side(v, below=False)

    def _part(self, low, high, v, below):
        """The fraction of the range [low, high] on one side of v, for low <= v <= high."""
        if high == low:
            return 0.0
        if self.kind == VARCHAR:                    # no arithmetic on strings: half, unless at the edge
            return 0.0 if (v <= low if below else v >= high) else 0.5
        return (v - low) / (high - low) if below else (high - v) / (high - low)

    def _side(self, v, below):
        if not self.comparable(v):
            return DEFAULT_RANGE
        if self.hist:
            total = sum(b[2] for b in self.hist)
            kept = 0.0
            for low, high, tuples, _ in self.hist:
                if (high < v) if below else (low > v):
                    kept += tuples                                  # the whole bucket
                elif low <= v <= high:
                    kept += tuples * self._part(low, high, v, below)
            return kept / total if total else 0.0
        if self.lo is not None:                     # no histogram: one bucket from min to max
            if v < self.lo:
                return 0.0 if below else 1.0
            if v > self.hi:
                return 1.0 if below else 0.0
            return self._part(self.lo, self.hi, v, below)
        return DEFAULT_RANGE

    def fraction(self, op, v):
        if op == '=':
            return self.equal(v)
        if op in ('<>', '!='):
            return 1.0 - self.equal(v)
        if op == '<':
            return self.below(v)
        if op == '>':
            return self.above(v)
        return min(1.0, (self.below(v) if op == '<=' else self.above(v)) + self.equal(v))

    def restrict(self, op, v):
        """This column as it is distributed among the rows that passed  c op v."""
        out = self.copy()
        if not self.comparable(v) or (self.ndv is None and not self.hist):
            if op == '=':
                out.ndv, out.lo, out.hi, out.hist = 1, v, v, None
            return out
        if op == '=':
            out.ndv, out.lo, out.hi = 1, v, v
            out.hist = [[v, v, 1, 1]] if self.hist else None
            return out
        if op in ('<>', '!='):
            return out
        below = op in ('<', '<=')
        if self.hist:
            kept = []
            for low, high, tuples, ndv in self.hist:
                if (high < v) if below else (low > v):
                    kept.append([low, high, tuples, ndv])
                elif low <= v <= high:
                    part = self._part(low, high, v, below)
                    if op in ('<=', '>='):
                        part = min(1.0, part + 1.0 / ndv)
                    if part > 0:
                        kept.append([low, v, tuples * part, max(1, round(ndv * part))] if below
                                    else [v, high, tuples * part, max(1, round(ndv * part))])
            out.hist = kept or None
            out.ndv = sum(b[3] for b in kept) or 1
            if kept:
                out.lo, out.hi = kept[0][0], kept[-1][1]
        else:
            share = self.fraction(op, v)
            out.ndv = max(1, round(self.ndv * share))
            if self.lo is not None:
                if below:
                    out.hi = min(self.hi, v)
                else:
                    out.lo = max(self.lo, v)
        return out


class Est:
    """rows, and the columns in output order: {name: ColEst}."""

    def __init__(self, rows, cols):
        self.rows = rows
        self.cols = cols

    def find(self, attr):
        """The name of the column an attribute of a condition refers to, or None."""
        if isinstance(attr, str):
            return attr if attr in self.cols else None
        if attr.relation is not None:
            name = f'{attr.relation}.{attr.name}'
            return name if name in self.cols else None
        hits = [n for n in self.cols if n == attr.name or n.endswith('.' + attr.name)]
        return hits[0] if len(hits) == 1 else None

    def width(self, names=None):
        return sum(c.width for n, c in self.cols.items() if names is None or n in names)

    def ndv(self, name):
        """Distinct values of a column here: never more than there are rows."""
        ndv = self.cols[name].ndv
        return None if ndv is None else max(1, min(ndv, self.rows))

    def with_rows(self, rows):
        return Est(rows, self.cols)


def conjuncts(tree):
    """The parts of a condition joined by AND at the top."""
    if isinstance(tree, Logic) and tree.op == 'and':
        return conjuncts(tree.left) + conjuncts(tree.right)
    return [tree]


def column_and_constant(est, node):
    """(column name, operator, constant) for  c op v  or  v op c, else None."""
    if not isinstance(node, Compare):
        return None
    left, op, right = node.left, node.op, node.right
    if isinstance(left, Literal) and isinstance(right, Attr):
        left, op, right = right, FLIP[op], left
    if isinstance(left, Attr) and isinstance(right, Literal):
        name = est.find(left)
        if name is not None:
            return name, op, right.value
    return None


def selectivity(est, node):
    """The fraction of est's rows that a condition keeps."""
    if isinstance(node, Logic):
        if node.op == 'not':
            return 1.0 - selectivity(est, node.child)
        p, q = selectivity(est, node.left), selectivity(est, node.right)
        return p * q if node.op == 'and' else p + q - p * q
    simple = column_and_constant(est, node)
    if simple is not None:
        name, op, value = simple
        return est.cols[name].fraction(op, value)
    if isinstance(node.left, Literal) and isinstance(node.right, Literal):
        a, b, op = node.left.value, node.right.value, node.op
        return float({'=': a == b, '<>': a != b, '!=': a != b, '<': a < b, '<=': a <= b,
                      '>': a > b, '>=': a >= b}[op])
    return two_columns(est, est, node)


def two_columns(left, right, node):
    """Selectivity of a comparison between two columns: of one input, or of the two sides of a join."""
    if node.op != '=':
        return DEFAULT_OTHER
    a = left.find(node.left) or right.find(node.left)
    b = right.find(node.right) or left.find(node.right)
    ndv_a = (left if a in left.cols else right).ndv(a) if a else None
    ndv_b = (right if b in right.cols else left).ndv(b) if b else None
    if ndv_a is None or ndv_b is None:
        return DEFAULT_OTHER
    return 1.0 / max(ndv_a, ndv_b)


def filtered(est, tree):
    """est after a filter: fewer rows, and the filtered columns cut down to what passed."""
    rows = float(est.rows)
    cols = dict(est.cols)
    current = Est(est.rows, cols)
    for part in conjuncts(tree):
        simple = column_and_constant(current, part)
        if simple is not None:
            name, op, value = simple
            rows *= cols[name].fraction(op, value)
            cols[name] = cols[name].restrict(op, value)
        else:
            rows *= selectivity(current, part)
    return Est(round_rows(rows), cols)
