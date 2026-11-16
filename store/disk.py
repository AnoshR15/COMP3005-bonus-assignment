"""Disk manager: the only code in the system that touches a file.

A database is a directory. It holds catalog.db and one file <table>.db per table. Every file is
a sequence of 4096-byte pages and nothing else: no file header. A page id is the zero-based
index of a page inside its file, so a page is named by (file name, page id).

Every read and every write of a page is counted here. Temporary files (sort runs, hash
partitions) are made of the same pages, live in a tmp directory inside the database directory,
and are counted by the same counters.
"""
import os

PAGE_SIZE = 4096
CATALOG_FILE = 'catalog.db'


class DiskManager:

    def __init__(self, directory):
        os.makedirs(directory, exist_ok=True)
        self.directory = directory
        self.files = {}             # file name -> open file object
        self.pages = {}             # file name -> number of pages allocated
        self.reads = 0
        self.writes = 0
        self.temps_created = 0
        self.live_temps = 0

    def path(self, name):
        return os.path.join(self.directory, name)

    def exists(self, name):
        return name in self.files or os.path.exists(self.path(name))

    def _open(self, name):
        f = self.files.get(name)
        if f is None:
            path = self.path(name)
            f = open(path, 'r+b' if os.path.exists(path) else 'w+b')
            self.files[name] = f
            self.pages[name] = os.path.getsize(path) // PAGE_SIZE
        return f

    def num_pages(self, name):
        if not self.exists(name):
            return 0
        self._open(name)
        return self.pages[name]

    def read_page(self, name, page_id):
        f = self._open(name)
        if not 0 <= page_id < self.pages[name]:
            raise ValueError(f'{name} has no page {page_id}; it has {self.pages[name]} pages')
        f.seek(page_id * PAGE_SIZE)
        data = f.read(PAGE_SIZE)
        self.reads += 1
        return data.ljust(PAGE_SIZE, b'\0')         # an allocated page never written reads as zeros

    def write_page(self, name, page_id, data):
        f = self._open(name)
        if not 0 <= page_id < self.pages[name]:
            raise ValueError(f'{name} has no page {page_id}; it has {self.pages[name]} pages')
        if len(data) != PAGE_SIZE:
            raise ValueError(f'a page is {PAGE_SIZE} bytes, not {len(data)}')
        f.seek(page_id * PAGE_SIZE)
        f.write(data)
        self.writes += 1

    def allocate_page(self, name):
        """The id of a new page at the end of the file. The file grows when the page is written."""
        self._open(name)
        self.pages[name] += 1
        return self.pages[name] - 1

    def create_temp(self):
        tmp = self.path('tmp')
        os.makedirs(tmp, exist_ok=True)
        self.temps_created += 1
        self.live_temps += 1
        return TempFile(self, os.path.join(tmp, f't{self.temps_created}.run'))

    def close(self):
        for f in self.files.values():
            f.close()
        self.files = {}
        tmp = self.path('tmp')
        if os.path.isdir(tmp) and not os.listdir(tmp):
            os.rmdir(tmp)


class TempFile:
    """A sequence of pages outside the database files: written front to back, read by page number."""

    def __init__(self, disk, path):
        self.disk = disk
        self.path = path
        self.num_pages = 0
        self.rows = 0                               # set by the writer, for reports
        self.file = open(path, 'w+b')

    def _handle(self):
        if self.file is None:                       # reopened after release()
            self.file = open(self.path, 'r+b')
        return self.file

    def append_page(self, data):
        f = self._handle()
        f.seek(self.num_pages * PAGE_SIZE)
        f.write(data)
        self.num_pages += 1
        self.disk.writes += 1

    def read_page(self, n):
        f = self._handle()
        f.seek(n * PAGE_SIZE)
        self.disk.reads += 1
        return f.read(PAGE_SIZE)

    def release(self):
        """Close the operating system handle; the file stays. A sort can own thousands of runs,
        more than a process may hold open at once."""
        if self.file is not None:
            self.file.close()
            self.file = None

    def delete(self):
        self.release()
        if os.path.exists(self.path):
            os.remove(self.path)
            self.disk.live_temps -= 1
