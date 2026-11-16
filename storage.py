#!/usr/bin/env python3
"""Storage engine command line, Module 1.

    python storage.py create-table --db DIR --name R --schema "a:INT,b:VARCHAR(20),c:INT"
    python storage.py load         --db DIR --table R --file r.csv
    python storage.py scan         --db DIR --table R --frames 8 [--limit N] --json
    python storage.py layout       --db DIR --table R --page 3
    python storage.py trace        --db DIR --file trace.txt --frames 16 --policy lru|clock --json
    python storage.py tables       --db DIR

scan --json     {"table": "R", "schema": [["a","INT"],["b","VARCHAR",20]], "rows": [[42,"bob"]],
                 "stats": {"hits":12,"misses":4,"evictions":1,"reads":4,"writes":0}}
trace --json    {"policy":"lru", "frames":16, "hits":812, "misses":188, "evictions":172,
                 "reads":188, "writes":0}

layout prints one page of a table's file: the header fields, the free space, and every slot as
number:(offset,length). --page is the page id, the zero-based index of the page in <table>.db.
Page 0 is a directory page, and its entries are printed instead.

trace replays a file of page ids, one per line, through a buffer pool: each page is fetched and
unpinned at once as clean. It reads a scratch file, not a table.

An error is one line starting with "error:" and the exit code is 1.
"""
import argparse
import json
import sys
import tempfile
from store import page
from store.buffer import BufferPool, BufferPoolError
from store.catalog import CatalogError
from store.db import Database, parse_schema, load_csv, is_database
from store.disk import DiskManager
from store.heap import DIR_HEADER, entry_at, is_directory_page
from store.record import RecordError


def open_database(a, must_exist=True):
    if must_exist and not is_database(a.db):
        raise CatalogError(f"there is no database in '{a.db}'")
    return Database(a.db, a.frames, a.policy)


def cmd_create_table(a):
    db = open_database(a, must_exist=False)
    db.create_table(a.name, parse_schema(a.schema))
    db.close()
    print(f'created table {a.name}')


def cmd_load(a):
    db = open_database(a)
    heap = db.table(a.table)
    try:
        count = load_csv(heap, a.file)
    finally:
        db.close()                          # rows loaded before an error stay loaded
    print(f'loaded {count} rows into {a.table}: {heap.info.num_tuples} rows on {heap.info.num_pages} data pages')


def cmd_scan(a):
    """Each row is written as the scan produces it and then forgotten. The command holds no list
    of rows, so what is in memory is what the pool holds: one directory page and one data page.
    The JSON object is therefore written in pieces, and the stats come last, after the rows."""
    db = open_database(a)
    heap = db.table(a.table)
    out = sys.stdout
    if a.json:
        out.write('{"table": ' + json.dumps(a.table, ensure_ascii=False)
                  + ', "schema": ' + json.dumps(heap.info.schema_json(), ensure_ascii=False) + ', "rows": [')
    else:
        print(heap.info.schema_text())
    count = 0
    if a.limit is None or a.limit > 0:
        scan = heap.scan()
        for _, row in scan:
            if a.json:
                out.write((', ' if count else '') + json.dumps(row, ensure_ascii=False))
            else:
                print(', '.join(repr(v) if isinstance(v, str) else str(v) for v in row))
            count += 1
            if a.limit is not None and count >= a.limit:
                break
        scan.close()                        # a scan stopped early still unpins its two pages
    stats = db.pool.stats()
    if a.json:
        out.write('], "stats": ' + json.dumps(stats) + ', "max_pinned": ' + str(db.pool.high_water) + '}\n')
    else:
        print(f'({count} rows)  ' + '  '.join(f'{k}={v}' for k, v in stats.items())
              + f'  max_pinned={db.pool.high_water}', file=sys.stderr)
    db.disk.close()                         # a scan changes nothing: nothing to flush


def cmd_layout(a):
    db = open_database(a)
    heap = db.table(a.table)
    pages = db.disk.num_pages(heap.file)
    if not 0 <= a.page < pages:
        raise CatalogError(f'{heap.file} has pages 0 to {pages - 1}')
    frame = db.pool.fetch_page(heap.file, a.page)
    data = frame.data
    if is_directory_page(a.page):
        page_id, next_dir, count = DIR_HEADER.unpack_from(data, 0)
        print(f'{heap.file} page {a.page}: directory page')
        print(f'page_id={page_id} next_dir_page={"none" if next_dir == page.NO_PAGE else next_dir} '
              f'entry_count={count}')
        for i in range(count):
            data_page, free = entry_at(data, i)
            print(f'  entry {i} at byte {10 + 6 * i}: data_page_id={data_page} free_bytes={free}')
    else:
        page_id, count, free_end = page.header(data)
        print(f'{heap.file} page {a.page}: data page')
        print(f'page_id={page_id} slot_count={count} free_end={free_end} free_space={page.free_space(data)}')
        print('slot directory: ' + (' '.join(f'{i}:({offset},{length})' for i, offset, length in page.slots(data))
                                    or 'empty'))
        for i, offset, length in page.slots(data):
            where = f'slot {i} at byte {page.HEADER_SIZE + page.SLOT_SIZE * i}'
            if offset == 0:
                print(f'  {where}: dead')
            else:
                record = bytes(data[offset:offset + length])
                print(f'  {where}: offset={offset} length={length}  {heap.codec.decode(record)}')
    db.pool.unpin_page(heap.file, a.page)
    db.disk.close()


def cmd_tables(a):
    db = open_database(a)
    for name in db.catalog.list_tables():
        info = db.catalog.get_table(name)
        print(f'{name} ({info.schema_text()})  file={info.file} pages={info.num_pages} tuples={info.num_tuples}')
    db.disk.close()


def run_trace(page_ids, frames, policy):
    """Fetch each page of a scratch file and unpin it at once as clean. Returns the pool's stats."""
    with tempfile.TemporaryDirectory() as scratch:
        disk = DiskManager(scratch)
        for _ in range(max(page_ids, default=-1) + 1):
            disk.allocate_page('trace.db')
        pool = BufferPool(disk, frames, policy)
        for page_id in page_ids:
            pool.fetch_page('trace.db', page_id)
            pool.unpin_page('trace.db', page_id, False)
        disk.close()
    return pool.stats()


def read_trace(path):
    page_ids = []
    with open(path) as f:
        for line_no, line in enumerate(f, start=1):
            text = line.strip()
            if text:
                if not text.isdigit():
                    raise ValueError(f"{path}, line {line_no}: '{text}' is not a page id")
                page_ids.append(int(text))
    return page_ids


def cmd_trace(a):
    page_ids = read_trace(a.file)
    stats = run_trace(page_ids, a.frames, a.policy)
    result = {'policy': a.policy, 'frames': a.frames, 'hits': stats['hits'], 'misses': stats['misses'],
              'evictions': stats['evictions'], 'reads': stats['reads'], 'writes': stats['writes'],
              'requests': len(page_ids)}
    if a.json:
        print(json.dumps(result))
    else:
        print('  '.join(f'{k}={v}' for k, v in result.items()))


def main(argv=None):
    p = argparse.ArgumentParser(prog='storage', description='Storage engine and buffer pool')
    commands = p.add_subparsers(dest='command', required=True)

    def command(name, func):
        c = commands.add_parser(name)
        c.set_defaults(func=func)
        c.add_argument('--db', '--catalog', dest='db', default='db', metavar='DIR',
                       help='database directory (default: db)')
        c.add_argument('--frames', type=int, default=16, help='buffer pool frames (default: 16)')
        c.add_argument('--policy', choices=['lru', 'clock'], default='lru')
        return c

    c = command('create-table', cmd_create_table)
    c.add_argument('--name', required=True)
    c.add_argument('--schema', required=True, help='for example "a:INT,b:VARCHAR(20)"')
    c = command('load', cmd_load)
    c.add_argument('--table', required=True)
    c.add_argument('--file', required=True)
    c = command('scan', cmd_scan)
    c.add_argument('--table', required=True)
    c.add_argument('--limit', type=int)
    c.add_argument('--json', action='store_true')
    c = command('layout', cmd_layout)
    c.add_argument('--table', required=True)
    c.add_argument('--page', type=int, required=True, help='page id: index of the page in the table file')
    command('tables', cmd_tables)
    c = command('trace', cmd_trace)
    c.add_argument('--file', required=True)
    c.add_argument('--json', action='store_true')

    a = p.parse_args(argv)
    try:
        a.func(a)
        return 0
    except (CatalogError, RecordError, BufferPoolError, ValueError) as e:
        print(f'error: {e}')
        return 1
    except OSError as e:
        print(f'error: cannot read {e.filename}: {e.strerror}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
