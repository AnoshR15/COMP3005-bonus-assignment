"""A database: one directory, one disk manager, one buffer pool, one catalog.

    db = Database('db', frames=16, policy='lru')
    heap = db.table('R')            # HeapFile
    db.close()                      # writes the tables' counts to the catalog and every dirty page
"""
import csv
import os
from .buffer import BufferPool
from .catalog import Catalog, CatalogError
from .disk import DiskManager, CATALOG_FILE
from .heap import HeapFile
from .record import INT, VARCHAR, RecordError


def is_database(directory):
    return os.path.exists(os.path.join(directory, CATALOG_FILE))


class Database:

    def __init__(self, directory, frames=16, policy='lru'):
        self.disk = DiskManager(directory)
        self.pool = BufferPool(self.disk, frames, policy)
        self.catalog = Catalog(self.pool)
        self.heaps = {}

    def table(self, name):
        if name not in self.heaps:
            info = self.catalog.get_table(name)
            if info is None:
                raise CatalogError(f"unknown table '{name}'")
            self.heaps[name] = HeapFile(self.pool, self.catalog, info)
        return self.heaps[name]

    def create_table(self, name, columns):
        self.catalog.create_table(name, columns)
        return self.table(name)

    def flush(self):
        for heap in self.heaps.values():
            heap.flush()
        self.pool.flush_all()

    def close(self):
        self.flush()
        self.disk.close()


def parse_schema(text):
    """ "a:INT,b:VARCHAR(20),c:INT" -> [('a', 'INT', None), ('b', 'VARCHAR', 20), ('c', 'INT', None)] """
    columns = []
    for part in text.split(','):
        name, colon, typ = part.strip().partition(':')
        name, typ = name.strip(), typ.strip().upper()
        if not colon or not name:
            raise CatalogError(f"'{part.strip()}' is not a column; expected name:TYPE")
        if typ == 'INT':
            columns.append((name, INT, None))
        elif typ.startswith('VARCHAR(') and typ.endswith(')') and typ[8:-1].isdigit():
            columns.append((name, VARCHAR, int(typ[8:-1])))
        else:
            raise CatalogError(f"column '{name}': '{typ}' is not a type; expected INT or VARCHAR(n)")
    return columns


def load_csv(heap, path):
    """Bulk load a CSV file into a table: no header line, one record per line, fields separated
    by commas, a field containing a comma wrapped in double quotes.

    A field that is wrong stops the load with an error naming the line and the column. The rows
    before that line stay loaded. Returns the number of rows inserted."""
    columns = heap.info.columns
    count = 0
    with open(path, newline='', encoding='utf8') as f:
        for line_no, fields in enumerate(csv.reader(f), start=1):
            if not fields:
                continue
            if len(fields) != len(columns):
                raise RecordError(f'line {line_no}: {len(fields)} fields, but table {heap.info.name} '
                                  f'has {len(columns)} columns')
            row = []
            for (name, kind, size), text in zip(columns, fields):
                if kind == INT:
                    try:
                        value = int(text)
                    except ValueError:
                        raise RecordError(f"line {line_no}, column {name}: '{text}' is not an integer") from None
                    if not -2 ** 31 <= value < 2 ** 31:
                        raise RecordError(f'line {line_no}, column {name}: {value} does not fit in a 4-byte INT')
                    row.append(value)
                else:
                    length = len(text.encode('utf-8'))
                    if length > size:
                        raise RecordError(f'line {line_no}, column {name}: the value is {length} bytes long, '
                                          f'more than VARCHAR({size}) allows')
                    row.append(text)
            try:
                heap.insert(tuple(row))
            except RecordError as e:
                raise RecordError(f'line {line_no}: {e}') from None
            count += 1
    return count
