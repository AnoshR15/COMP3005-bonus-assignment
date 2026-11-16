"""ANALYZE: exact statistics of one table, stored in its catalog entry.

    n_tuples   live rows                      n_pages   data pages, without directory pages
    width      average record length, bytes
    per column:
        ndv        number of distinct values
        min, max
        width      average encoded length of the column, bytes   (an addition: it is what lets a
                   projection's output be sized)
        hist       equi-depth histogram: a list of buckets [low, high, tuples, distinct values]

How it is computed. The table is scanned once. During that scan the rows are counted and
measured, and every column is written to its own temporary file. Each column file is then sorted
with the external sort of the execution engine, and one walk over the sorted values gives the
number of distinct values, the lowest and highest, and the histogram. Nothing is sampled, and at
no point is a column held in memory: the scan holds one frame, each column file one frame while
it is written, and the sort works inside the buffer pool's frames like any other sort.

(A table with more columns than frames - 1 is scanned once per batch of frames - 1 columns.)

The histogram. Equi-depth: cut the sorted column every n / k values, k being --buckets. Two
refinements, both about equal values:
    - a cut is never made inside a run of equal values, so no value is in two buckets and the
      bucket holding a value is unambiguous;
    - a value whose run alone is at least n / k tuples gets a bucket to itself, so the estimate
      for a very common value is its exact count, not an average with its neighbours.
So buckets hold roughly n / k tuples, more when a common value overshoots, and there can be a
few more or fewer than k of them.

The statistics are a plain dictionary and go into the catalog entry's "stats" member. Running
ANALYZE again on an unchanged table produces the same dictionary and so the same catalog bytes.
"""
import math
from executor.context import ExecContext, ExecError, Counters, RunWriter, codec_for
from executor.operators import SeqScan, FileScan, Column
from executor.sort import Sort
from store.record import VARCHAR

DEFAULT_BUCKETS = 20


def value_runs(pull):
    """(value, how many times in a row) from a sorted stream of one-column rows."""
    row = pull()
    while row is not None:
        value, count = row[0], 1
        row = pull()
        while row is not None and row[0] == value:
            count += 1
            row = pull()
        yield value, count


def build_histogram(runs, n, buckets):
    """-> (ndv, lowest, highest, histogram) from the (value, count) runs of a sorted column."""
    depth = max(1, math.ceil(n / buckets))
    histogram = []
    current = None                      # [low, high, tuples, distinct values]
    ndv, lowest, highest = 0, None, None
    for value, count in runs:
        ndv += 1
        if lowest is None:
            lowest = value
        highest = value
        if current is not None and count >= depth:      # a common value: close what came before it
            histogram.append(current)
            current = None
        if current is None:
            current = [value, value, 0, 0]
        current[1] = value
        current[2] += count
        current[3] += 1
        if current[2] >= depth:
            histogram.append(current)
            current = None
    if current is not None:
        histogram.append(current)
    return ndv, lowest, highest, histogram


def analyze(db, table, buckets=DEFAULT_BUCKETS):
    """Compute the statistics of a table, store them in the catalog, and return them."""
    info = db.catalog.get_table(table)
    if info is None:
        raise ExecError(f"unknown table '{table}'")
    if buckets < 1:
        raise ExecError('--buckets must be at least 1')
    frames = db.pool.size
    if frames < 3:
        raise ExecError('ANALYZE sorts each column and needs at least 3 frames')
    ctx = ExecContext(db)
    counters = Counters()
    columns = [Column(name, kind, size) for name, kind, size in info.columns]
    row_codec = info.codec()
    files = [None] * len(columns)
    widths = [0] * len(columns)
    n = total_bytes = 0

    # One scan: count and measure the rows, and split them into one file per column.
    batch = frames - 1
    for start in range(0, len(columns), batch):
        chosen = range(start, min(start + batch, len(columns)))
        scan = SeqScan(ctx, table, 'analyze')
        scan.assign(1, 1)
        writers = []
        try:
            for i in chosen:
                writers.append(RunWriter(ctx, codec_for([columns[i]]), counters))
            scan.open()
            n = 0
            while True:
                row = scan.next()
                if row is None:
                    break
                n += 1
                if start == 0:
                    total_bytes += row_codec.size(row)
                for writer, i in zip(writers, chosen):
                    value = row[i]
                    writer.add((value,))
                    widths[i] += 2 + len(value.encode('utf-8')) if columns[i].kind == VARCHAR else 4
        finally:
            scan.close()
            counters.page_reads += scan.c.page_reads
            for writer, i in zip(writers, chosen):
                files[i] = writer.close()

    # Each column: external sort, then one walk over the sorted values.
    stats = {'n_tuples': n, 'n_pages': info.num_pages, 'width': round(total_bytes / n, 2) if n else 0,
             'buckets': buckets, 'columns': {}}
    try:
        for i, column in enumerate(columns):
            if files[i] is None:                        # an empty table
                stats['columns'][column.name] = {'ndv': 0, 'min': None, 'max': None, 'width': 0, 'hist': []}
                continue
            sorter = Sort(ctx, FileScan(ctx, files[i], [column], counters), [(0, False)], 'analyze',
                          counters=counters)
            sorter.assign(frames, frames)
            try:
                sorter.open()
                ndv, lowest, highest, histogram = build_histogram(value_runs(sorter._next), n, buckets)
            finally:
                sorter.close()
                files[i].delete()
                files[i] = None
            stats['columns'][column.name] = {'ndv': ndv, 'min': lowest, 'max': highest,
                                             'width': round(widths[i] / n, 2), 'hist': histogram}
    finally:
        for file in files:
            if file is not None:
                file.delete()
    info.stats = stats
    db.catalog.save(info)
    db.pool.flush_all()
    stats['io'] = counters.as_dict()                    # for the caller to report; not stored
    return stats
