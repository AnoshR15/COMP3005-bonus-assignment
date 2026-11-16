"""The measurements of REPORT_M1.md. Every number in that report comes from the files this writes.

    python -m bench.m1_measure                     all studies
    python -m bench.m1_measure capacity policies   only the named ones: capacity policies scan dump
    python -m bench.m1_measure --traces DIR ...    where to write trace1.txt .. trace5.txt (default: traces)

Writes results/m1/*.csv, results/m1/tables.md and results/m1/page_dump.txt. Builds its tables in
bench/m1db, from scratch on every run.

Traces. No trace files were published with the specification, so the five traces are my own
(my_trace below). The policies study writes them to the traces directory as trace1.txt ..
trace5.txt, one page id per line, together with example.txt, the six-fetch trace of Section 5.3,
and then measures the files it wrote. Trace 3 is the one the specification describes: an 80-page
file scanned ten times.
"""
import csv
import os
import random
import shutil
import sys
from storage import run_trace, read_trace
from store import page
from store.catalog import HEADER as CATALOG_HEADER
from store.db import Database
from store.disk import PAGE_SIZE
from store.heap import DIR_HEADER, entry_at, data_page_ids

DB = os.path.join('bench', 'm1db')
OUT = os.path.join('results', 'm1')
BUDGETS = (4, 16, 64, 256)


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

    def save(self):
        with open(os.path.join(OUT, 'tables.md'), 'w', encoding='utf8') as f:
            f.write('\n\n'.join(self.parts) + '\n')


def fresh_database(frames=64):
    shutil.rmtree(DB, ignore_errors=True)
    return Database(DB, frames)


# ------------------------------------------------------------------ 1. capacity

def slot_counts(db, heap):
    """Number of slots on each data page of a table, in directory order."""
    counts = []
    for page_id in data_page_ids(db.disk.num_pages(heap.file)):     # not the directory pages
        frame = db.pool.fetch_page(heap.file, page_id)
        counts.append(page.slot_count(frame.data))
        db.pool.unpin_page(heap.file, page_id)
    return counts


def study_capacity(tables):
    rng = random.Random(1)
    names = ['amy', 'bob', 'chandra', 'dmitri', 'eleanor', 'fatima', 'george', 'hiroshi', 'ingrid',
             'jo', 'katarzyna', 'li', 'mohammed', 'nnamdi', 'olga', 'priya', 'quentin', 'rosalind']
    cases = [
        ('all INT', 'a:INT, b:INT, c:INT', [('a', 'INT', None), ('b', 'INT', None), ('c', 'INT', None)],
         lambda i: (i, i * 2, i * 3)),
        ('short VARCHAR, every value 3 bytes', 'a:INT, b:VARCHAR(20), c:INT',
         [('a', 'INT', None), ('b', 'VARCHAR', 20), ('c', 'INT', None)],
         lambda i: (i, 'bob', i)),
        ('short VARCHAR, values of 2 to 9 bytes', 'a:INT, b:VARCHAR(20), c:INT',
         [('a', 'INT', None), ('b', 'VARCHAR', 20), ('c', 'INT', None)],
         lambda i: (i, rng.choice(names), i)),
        ('VARCHAR(200), every value 200 bytes', 'k:INT, pad:VARCHAR(200)',
         [('k', 'INT', None), ('pad', 'VARCHAR', 200)],
         lambda i: (i, f'{i:06d}' + 'p' * 194)),
    ]
    rows = []
    db = fresh_database()
    for n, (label, schema, columns, make) in enumerate(cases):
        heap = db.create_table(f'C{n}', columns)
        total_bytes = 0
        count = 20000
        for i in range(count):
            row = make(i)
            total_bytes += heap.codec.size(row)
            heap.insert(row)
        mean = total_bytes / count
        computed = int(page.CAPACITY // (mean + page.SLOT_SIZE))
        counts = slot_counts(db, heap)
        full = counts[:-1]                              # the last page is whatever was left over
        measured = sum(full) / len(full)
        rows.append([label, schema, f'{mean:.2f}'.rstrip('0').rstrip('.'), computed,
                     f'{measured:.2f}'.rstrip('0').rstrip('.'), min(full), max(full), heap.info.num_pages])
    db.close()
    tables.add('Records per page', ['data', 'schema', 'record length L, bytes (mean)',
                                    'computed floor(4088 / (L + 4))', 'measured, mean over full pages',
                                    'fewest on a page', 'most on a page', 'data pages for 20,000 rows'],
               rows, 'capacity')


# ------------------------------------------------------------------ 2. policies

EXAMPLE = [1, 2, 3, 1, 4, 2]            # the worked trace of Section 5.3 of the specification


def my_trace(number):
    """One of the five traces of the report: (what it is, page ids). No traces were published
    with the specification, so these are mine. Each is built to show one behaviour, and the
    random ones have a fixed seed, so the files and every number in the report are the same on
    every run."""
    rng = random.Random(100 + number)
    if number == 1:
        return 'uniform random over 300 pages', [rng.randrange(300) for _ in range(5000)]
    if number == 2:
        return '80 percent of requests to 60 hot pages of 300', \
            [rng.randrange(60) if rng.random() < 0.8 else 60 + rng.randrange(240) for _ in range(5000)]
    if number == 3:
        return '80-page file scanned ten times, as specified', list(range(80)) * 10
    if number == 4:
        ids = []
        for _ in range(10):                     # a working set of 30 pages, interrupted by a scan
            ids += [rng.randrange(30) for _ in range(300)]
            ids += list(range(100, 300))
        return 'working set of 30 pages, a 200-page scan every 300 requests', ids
    weights = [1 / (i + 1) for i in range(500)]
    return 'skewed: page i requested in proportion to 1/(i+1), 500 pages', \
        rng.choices(range(500), weights=weights, k=5000)


def write_trace(path, page_ids):
    with open(path, 'w', newline='\n') as f:
        f.write(''.join(f'{p}\n' for p in page_ids))


def study_policies(tables, trace_dir):
    """Writes the five trace files, then measures them as files: what the table reports is what
    `storage trace --file traces/traceN.txt` prints."""
    os.makedirs(trace_dir, exist_ok=True)
    write_trace(os.path.join(trace_dir, 'example.txt'), EXAMPLE)
    rows, described = [], []
    for number in range(1, 6):
        what, generated = my_trace(number)
        path = os.path.join(trace_dir, f'trace{number}.txt')
        write_trace(path, generated)
        label, ids = f'trace {number}', read_trace(path)
        described.append([label, what, len(ids), len(set(ids))])
        for b in BUDGETS:
            lru = run_trace(ids, b, 'lru')
            clock = run_trace(ids, b, 'clock')
            rows.append([label, b, len(ids), lru['hits'], f"{100 * lru['hits'] / len(ids):.1f}%", lru['evictions'],
                         clock['hits'], f"{100 * clock['hits'] / len(ids):.1f}%", clock['evictions']])
    tables.add('The traces', ['trace', 'what it is', 'requests', 'distinct pages'], described, 'traces')
    tables.add('Hit rate under both policies', ['trace', 'B', 'requests', 'LRU hits', 'LRU hit rate',
                                                'LRU evictions', 'CLOCK hits', 'CLOCK hit rate',
                                                'CLOCK evictions'], rows, 'policies')


# ------------------------------------------------------------------ 3. scan cost

def study_scan(tables):
    db = fresh_database()
    heap = db.create_table('Big', [('a', 'INT', None), ('b', 'INT', None), ('c', 'INT', None)])
    for i in range(255 * 500):
        heap.insert((i, i, i))
    assert heap.info.num_pages == 500
    catalog_pages = db.disk.num_pages('catalog.db')
    file_pages = db.disk.num_pages('Big.db')
    db.close()
    rows = []
    for b in (2, 8, 64):
        db = Database(DB, b)
        count = sum(1 for _ in db.table('Big').scan())
        s = db.pool.stats()
        rows.append([b, count, s['reads'], s['hits'], s['misses'], s['evictions'], s['writes'], db.pool.high_water])
        db.disk.close()
    tables.add(f'Scan of a 500-page table ({file_pages} pages in the file: 500 data and 1 directory; '
               f'the catalog is {catalog_pages} page)',
               ['B', 'rows', 'page reads', 'hits', 'misses', 'evictions', 'writes', 'most pages pinned at once'],
               rows, 'scan')


# ------------------------------------------------------------------ a real page, byte by byte

def hexdump(data, start, stop):
    lines = []
    for at in range(start - start % 16, stop, 16):
        chunk = data[at:at + 16]
        text = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{at:5d}  {chunk[:8].hex(" ")}  {chunk[8:].hex(" ")}  {text}')
    return '\n'.join(lines)


def study_dump(tables):
    """The page of Section 3.2 as it is in the file: rows of 13, 20 and 13 bytes, slot 1 deleted."""
    db = fresh_database()
    heap = db.create_table('R', [('a', 'INT', None), ('b', 'VARCHAR', 20), ('c', 'INT', None)])
    heap.insert((42, 'bob', 7))
    middle = heap.insert((1, 'abcdefghij', 2))
    heap.insert((43, 'amy', 9))
    heap.delete(middle)
    db.close()
    with open(os.path.join(DB, 'R.db'), 'rb') as f:
        directory, data = f.read(PAGE_SIZE), f.read(PAGE_SIZE)
    with open(os.path.join(DB, 'catalog.db'), 'rb') as f:
        catalog = f.read(PAGE_SIZE)
    _, _, free_end = page.header(data)
    length = CATALOG_HEADER.unpack_from(catalog, 0)[2]
    out = ['R.db, page 1 (data page), bytes 0 to 31: header and slot directory',
           hexdump(data, 0, 32), '',
           f'R.db, page 1, bytes {free_end - free_end % 16} to 4095: record data',
           hexdump(data, free_end, PAGE_SIZE), '',
           'R.db, page 0 (directory page), bytes 0 to 15',
           hexdump(directory, 0, 16), '',
           f'catalog.db, page 0, bytes 0 to {12 + length - 1}: header and entry text',
           hexdump(catalog, 0, 12 + length), '']
    assert DIR_HEADER.unpack_from(directory, 0) == (0, page.NO_PAGE, 1) and entry_at(directory, 0) == (1, 4030)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'page_dump.txt'), 'w', encoding='utf8') as f:
        f.write('\n'.join(out))
    print('\n'.join(out), flush=True)


STUDIES = ('capacity', 'policies', 'scan', 'dump')


def main():
    args = sys.argv[1:]
    trace_dir = 'traces'
    if '--traces' in args:
        at = args.index('--traces')
        trace_dir = args[at + 1]
        del args[at:at + 2]
    wanted = args or list(STUDIES)
    tables = Tables()
    for name in wanted:
        if name == 'policies':
            study_policies(tables, trace_dir)
        else:
            {'capacity': study_capacity, 'scan': study_scan, 'dump': study_dump}[name](tables)
    if len(wanted) == len(STUDIES):
        tables.save()
    shutil.rmtree(DB, ignore_errors=True)


if __name__ == '__main__':
    main()
