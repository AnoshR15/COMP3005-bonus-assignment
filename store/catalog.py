"""The catalog: what tables exist, their schemas, their files and their sizes. Lives in
catalog.db and is read and written through the buffer pool like any other page.

One entry per table. An entry is a piece of JSON text

    {"name": "R", "columns": [["a", "INT", null], ["b", "VARCHAR", 20]],
     "file": "R.db", "pages": 8, "tuples": 2040, "stats": {...}}

    file     the table's data file          pages   number of data pages in it
    tuples   number of live rows            stats   written by ANALYZE (Module 4); absent until then

stored in one or more linked catalog pages:

    offset  size  field
         0     4  page_id      uint32
         4     2  kind         uint16   0 unused, 1 first page of an entry, 2 continuation page
         6     2  length       uint16   bytes of entry text in this page
         8     4  next_page    uint32   the entry's next page, 0xFFFFFFFF if none
        12   ...  entry text   UTF-8, up to 4084 bytes

Catalog pages are not slotted pages, on purpose. An entry is rewritten every time a table's
counts or statistics change, and a slotted page never gives back the space of a deleted record.
Here an entry is rewritten in place, in the pages it already has. It takes more pages when it
grows (statistics for many columns) and hands back pages (kind 0) when it shrinks.
"""
import json
import struct
from . import page
from .disk import PAGE_SIZE, CATALOG_FILE
from .record import INT, VARCHAR, RecordCodec

HEADER = struct.Struct('<IHHI')         # page_id, kind, length, next_page
HEADER_SIZE = HEADER.size               # 12
TEXT_CAPACITY = PAGE_SIZE - HEADER_SIZE
UNUSED, FIRST, CONTINUATION = 0, 1, 2


class CatalogError(Exception):
    pass


class TableInfo:

    def __init__(self, name, columns, file, pages=0, tuples=0, stats=None):
        self.name = name
        self.columns = [tuple(c) for c in columns]      # (name, kind, size)
        self.file = file
        self.num_pages = pages                          # data pages
        self.num_tuples = tuples                        # live rows
        self.stats = stats
        self.catalog_pages = []                         # the catalog pages holding this entry

    def to_text(self):
        entry = {'name': self.name, 'columns': [list(c) for c in self.columns], 'file': self.file,
                 'pages': self.num_pages, 'tuples': self.num_tuples}
        if self.stats is not None:
            entry['stats'] = self.stats
        return json.dumps(entry, separators=(',', ':')).encode('utf-8')

    def codec(self):
        return RecordCodec([(kind, size) for _, kind, size in self.columns])

    def schema_json(self):
        """[["a","INT"],["b","VARCHAR",20]]"""
        return [[n, k, s] if k == VARCHAR else [n, k] for n, k, s in self.columns]

    def schema_text(self):
        return ', '.join(f"{n}:{k if k == INT else f'{k}({s})'}" for n, k, s in self.columns)


def valid_table_name(name):
    return bool(name) and (name[0].isalpha() or name[0] == '_') and \
        all(c.isalnum() or c == '_' for c in name)


class Catalog:

    def __init__(self, pool):
        self.pool = pool
        self.tables = None                              # name -> TableInfo, read on first use

    def _load(self):
        if self.tables is None:
            self.tables = {}
            for page_id in range(self.pool.disk.num_pages(CATALOG_FILE)):
                frame = self.pool.fetch_page(CATALOG_FILE, page_id)
                kind = HEADER.unpack_from(frame.data, 0)[1]
                self.pool.unpin_page(CATALOG_FILE, page_id)
                if kind == FIRST:
                    info = self._read_entry(page_id)
                    self.tables[info.name] = info
        return self.tables

    def _read_entry(self, first_page):
        text, pages, page_id = b'', [], first_page
        while page_id != page.NO_PAGE:
            frame = self.pool.fetch_page(CATALOG_FILE, page_id)
            _, _, length, next_page = HEADER.unpack_from(frame.data, 0)
            text += bytes(frame.data[HEADER_SIZE:HEADER_SIZE + length])
            self.pool.unpin_page(CATALOG_FILE, page_id)
            pages.append(page_id)
            page_id = next_page
        info = TableInfo(**json.loads(text.decode('utf-8')))
        info.catalog_pages = pages
        return info

    def list_tables(self):
        return sorted(self._load())

    def get_table(self, name):
        """The table's TableInfo, or None when there is no such table."""
        return self._load().get(name)

    def create_table(self, name, columns):
        """columns is a list of (name, kind, size); size is None for INT."""
        if not valid_table_name(name) or name == 'catalog':
            raise CatalogError(f"'{name}' cannot be a table name: letters, digits and _ only, "
                               "not starting with a digit, and not 'catalog'")
        if name in self._load():
            raise CatalogError(f"table '{name}' already exists")
        file = name + '.db'
        if self.pool.disk.exists(file):
            raise CatalogError(f"a file named {file} is already in the database directory")
        if not columns:
            raise CatalogError('a table needs at least one column')
        seen = set()
        for column, kind, size in columns:
            if column in seen:
                raise CatalogError(f"table '{name}' has two columns named '{column}'")
            seen.add(column)
            if kind not in (INT, VARCHAR) or (kind == VARCHAR and not 1 <= size <= 255):
                raise CatalogError(f"column '{column}': the type must be INT or VARCHAR(n) with n from 1 to 255")
        from .heap import init_directory
        directory = self.pool.new_page(file)            # page 0: the first directory page, empty
        init_directory(directory.data, directory.page_id)
        self.pool.unpin_page(file, directory.page_id, True)
        info = TableInfo(name, columns, file)
        self.save(info)
        self.tables[name] = info
        return info

    def save(self, info):
        """Write a table's entry, in the pages it already has and as many more as it needs."""
        text = info.to_text()
        pieces = [text[i:i + TEXT_CAPACITY] for i in range(0, len(text), TEXT_CAPACITY)]
        pages = list(info.catalog_pages)
        while len(pages) < len(pieces):
            pages.append(self._free_page(pages))
        for n, page_id in enumerate(pages):
            frame = self.pool.fetch_page(CATALOG_FILE, page_id)
            frame.data[:] = bytes(PAGE_SIZE)
            if n < len(pieces):
                next_page = pages[n + 1] if n + 1 < len(pieces) else page.NO_PAGE
                HEADER.pack_into(frame.data, 0, page_id, FIRST if n == 0 else CONTINUATION,
                                 len(pieces[n]), next_page)
                frame.data[HEADER_SIZE:HEADER_SIZE + len(pieces[n])] = pieces[n]
            else:                                       # the entry shrank: this page is free again
                HEADER.pack_into(frame.data, 0, page_id, UNUSED, 0, page.NO_PAGE)
            self.pool.unpin_page(CATALOG_FILE, page_id, True)
        info.catalog_pages = pages[:len(pieces)]

    def _free_page(self, taken):
        """A catalog page nobody uses: an unused one if there is one, otherwise a new page."""
        for page_id in range(self.pool.disk.num_pages(CATALOG_FILE)):
            if page_id in taken:
                continue
            frame = self.pool.fetch_page(CATALOG_FILE, page_id)
            kind = HEADER.unpack_from(frame.data, 0)[1]
            self.pool.unpin_page(CATALOG_FILE, page_id)
            if kind == UNUSED:
                return page_id
        frame = self.pool.new_page(CATALOG_FILE)
        HEADER.pack_into(frame.data, 0, frame.page_id, UNUSED, 0, page.NO_PAGE)
        self.pool.unpin_page(CATALOG_FILE, frame.page_id, True)
        return frame.page_id
