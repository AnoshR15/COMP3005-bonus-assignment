"""Heap file: the rows of one table, in slotted pages, in its own file <table>.db.

The file is a linked list of directory pages, each listing data pages and their free space.
Page 0 is the first directory page.

    directory page
    offset  size  field
         0     4  page_id          uint32
         4     4  next_dir_page    uint32, 0xFFFFFFFF if none
         8     2  entry_count      uint16
        10     6  entry 0          uint32 data_page_id, uint16 free_bytes
        16     6  entry 1
       ...
    681 entries fit: floor((4096 - 10) / 6)

free_bytes is the data page's free space, free_end - (8 + 4 * slot_count), kept up to date by
every insert.

Pages are only ever added at the end of the file, and a new directory page is added exactly when
the last one is full. So the file is always: directory, 681 data pages, directory, 681 data
pages, ... and page p is a directory page exactly when p is a multiple of 682. The directory
order of the data pages is their page id order.

A record id (rid) is (page id, slot number).
"""
import struct
from . import page
from .disk import PAGE_SIZE
from .record import RecordError

DIR_HEADER = struct.Struct('<IIH')      # page_id, next_dir_page, entry_count
DIR_ENTRY = struct.Struct('<IH')        # data_page_id, free_bytes
DIR_HEADER_SIZE = DIR_HEADER.size       # 10
DIR_ENTRY_SIZE = DIR_ENTRY.size         # 6
DIR_CAPACITY = (PAGE_SIZE - DIR_HEADER_SIZE) // DIR_ENTRY_SIZE      # 681
DIR_SPACING = DIR_CAPACITY + 1          # a directory page every 682 pages
MAX_RECORD = page.CAPACITY - page.SLOT_SIZE     # the longest record an empty page can take


def init_directory(buf, page_id):
    buf[:] = bytes(PAGE_SIZE)
    DIR_HEADER.pack_into(buf, 0, page_id, page.NO_PAGE, 0)


def is_directory_page(page_id):
    return page_id % DIR_SPACING == 0


def data_page_ids(file_pages):
    """The data pages of a table file with `file_pages` pages, in directory order."""
    return [p for p in range(1, file_pages) if p % DIR_SPACING]


def entry_at(buf, index):
    return DIR_ENTRY.unpack_from(buf, DIR_HEADER_SIZE + DIR_ENTRY_SIZE * index)


class HeapFile:

    def __init__(self, pool, catalog, info):
        self.pool = pool
        self.catalog = catalog
        self.info = info
        self.file = info.file
        self.codec = info.codec()
        self.names = [name for name, _, _ in info.columns]
        self.changed = False
        # First fit starts at the front of the directory every time, in principle. Free space
        # only ever shrinks (deleting frees nothing), so once the entries before some position
        # were too small for a record of `need` bytes, they are too small for any record at
        # least that long. The hint remembers one such (need, directory page, entry index).
        self.hint = None

    # ---------------------------------------------------------------- rows

    def insert(self, row):
        """Store a row: first fit over the directory, a new data page if nothing fits."""
        self.codec.check(row, self.names)
        record = self.codec.encode(row)
        if len(record) > MAX_RECORD:
            raise RecordError(f'table {self.info.name}, row {self.info.num_tuples + 1}: a record of '
                              f'{len(record)} bytes does not fit in a page ({MAX_RECORD} bytes at most)')
        need = len(record) + page.SLOT_SIZE
        pool, file = self.pool, self.file
        fresh = self.hint is None or need < self.hint[0]        # must look from the very front
        dir_id, start = (0, 0) if fresh else self.hint[1:]
        while True:
            directory = pool.fetch_page(file, dir_id)
            _, next_dir, count = DIR_HEADER.unpack_from(directory.data, 0)
            for i in range(start, count):
                data_id, free = entry_at(directory.data, i)
                if free >= need:                                # first fit
                    frame = pool.fetch_page(file, data_id)
                    slot = page.insert(frame.data, record)
                    DIR_ENTRY.pack_into(directory.data, DIR_HEADER_SIZE + DIR_ENTRY_SIZE * i,
                                        data_id, page.free_space(frame.data))
                    pool.unpin_page(file, data_id, True)
                    pool.unpin_page(file, dir_id, True)
                    return self._inserted(need, fresh, dir_id, i, data_id, slot)
            if next_dir == page.NO_PAGE:
                break                                           # keep the last directory page pinned
            pool.unpin_page(file, dir_id)
            dir_id, start = next_dir, 0
        # Nothing fits: a new data page, listed in the last directory page, or in a new one.
        if count == DIR_CAPACITY:
            more = pool.new_page(file)
            init_directory(more.data, more.page_id)
            DIR_HEADER.pack_into(directory.data, 0, dir_id, more.page_id, count)
            pool.unpin_page(file, dir_id, True)
            directory, dir_id, count = more, more.page_id, 0
        frame = pool.new_page(file)
        data_id = frame.page_id
        page.init(frame.data, data_id)
        slot = page.insert(frame.data, record)
        DIR_ENTRY.pack_into(directory.data, DIR_HEADER_SIZE + DIR_ENTRY_SIZE * count,
                            data_id, page.free_space(frame.data))
        struct.pack_into('<H', directory.data, 8, count + 1)
        pool.unpin_page(file, data_id, True)
        pool.unpin_page(file, dir_id, True)
        self.info.num_pages += 1
        return self._inserted(need, fresh, dir_id, count, data_id, slot)

    def _inserted(self, need, fresh, dir_id, index, data_id, slot):
        if fresh or need == self.hint[0]:
            self.hint = (need, dir_id, index)
        self.info.num_tuples += 1
        self.changed = True
        return (data_id, slot)

    def get(self, rid):
        """The row at a record id. Reads one page."""
        page_id, slot = rid
        frame = self._data_page(page_id)
        try:
            return self.codec.decode(page.get(frame.data, slot))
        except page.PageError as e:
            raise RecordError(f'table {self.info.name}: no row at rid ({page_id}, {slot}): {e}') from None
        finally:
            self.pool.unpin_page(self.file, page_id)

    def delete(self, rid):
        """Mark the row's slot dead, and bring the page's directory entry up to date. The space
        is not reclaimed, so the entry's free_bytes is in fact what it was."""
        page_id, slot = rid
        frame = self._data_page(page_id)
        try:
            page.delete(frame.data, slot)
        except page.PageError as e:
            self.pool.unpin_page(self.file, page_id)
            raise RecordError(f'table {self.info.name}: no row at rid ({page_id}, {slot}): {e}') from None
        free = page.free_space(frame.data)
        self.pool.unpin_page(self.file, page_id, True)
        dir_id = (page_id // DIR_SPACING) * DIR_SPACING         # the directory page that lists it
        index = page_id % DIR_SPACING - 1
        directory = self.pool.fetch_page(self.file, dir_id)
        changed = entry_at(directory.data, index) != (page_id, free)
        if changed:
            DIR_ENTRY.pack_into(directory.data, DIR_HEADER_SIZE + DIR_ENTRY_SIZE * index, page_id, free)
        self.pool.unpin_page(self.file, dir_id, changed)
        self.info.num_tuples -= 1
        self.changed = True

    def scan(self):
        """Every live row as (rid, row), in directory order.

        At most one directory page and one data page are pinned at any instant, and the data
        page is unpinned before the next one is fetched, also when the caller stops early."""
        pool, file, decode = self.pool, self.file, self.codec.decode
        dir_id = 0
        while dir_id != page.NO_PAGE:
            directory = pool.fetch_page(file, dir_id)
            try:
                _, next_dir, count = DIR_HEADER.unpack_from(directory.data, 0)
                for i in range(count):
                    data_id, _ = entry_at(directory.data, i)
                    frame = pool.fetch_page(file, data_id)
                    try:
                        for slot, record in page.records(frame.data):
                            yield (data_id, slot), decode(record)
                    finally:
                        pool.unpin_page(file, data_id)
            finally:
                pool.unpin_page(file, dir_id)
            dir_id = next_dir

    def flush(self):
        """Write the table's page and row counts back to the catalog."""
        if self.changed:
            self.catalog.save(self.info)
            self.changed = False

    def _data_page(self, page_id):
        if is_directory_page(page_id) or not 0 < page_id < self.pool.disk.num_pages(self.file):
            raise RecordError(f'table {self.info.name}: page {page_id} is not a data page')
        return self.pool.fetch_page(self.file, page_id)
