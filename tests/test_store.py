"""The storage engine against the Module 1 specification: record and page formats, heap files,
the buffer pool with both policies, the catalog, the loader and the command line.

The checklist of Section 10 is the class ChecklistTests."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import storage
from tests.base import ROOT
from store import page
from store.buffer import BufferPool, BufferPoolError
from store.catalog import CatalogError
from store.db import Database, parse_schema, load_csv
from store.disk import DiskManager, PAGE_SIZE
from store.heap import DIR_HEADER, DIR_CAPACITY, entry_at, data_page_ids, is_directory_page
from store.record import RecordCodec, RecordError
from storage import run_trace

R = [('a', 'INT', None), ('b', 'VARCHAR', 20), ('c', 'INT', None)]
INTS = [('a', 'INT', None), ('b', 'INT', None), ('c', 'INT', None)]


class TempDirCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


def state(buf):
    """(slot_count, free_end, free space, slot directory) of a page."""
    _, count, free_end = page.header(buf)
    return count, free_end, page.free_space(buf), [(o, n) for _, o, n in page.slots(buf)]


class ChecklistTests(TempDirCase):

    def test_the_page_example_reproduces_exactly(self):
        """Section 3.2: three inserts of 13, 20 and 13 bytes, then a delete of slot 1."""
        buf = bytearray(PAGE_SIZE)
        page.init(buf, 7)
        self.assertEqual(state(buf), (0, 4096, 4088, []))
        self.assertEqual(page.insert(buf, b'a' * 13), 0)
        self.assertEqual(state(buf), (1, 4083, 4071, [(4083, 13)]))
        self.assertEqual(page.insert(buf, b'b' * 20), 1)
        self.assertEqual(state(buf), (2, 4063, 4047, [(4083, 13), (4063, 20)]))
        self.assertEqual(page.insert(buf, b'c' * 13), 2)
        self.assertEqual(state(buf), (3, 4050, 4030, [(4083, 13), (4063, 20), (4050, 13)]))
        page.delete(buf, 1)
        self.assertEqual(state(buf), (3, 4050, 4030, [(4083, 13), (0, 0), (4050, 13)]))    # nothing freed
        # the header and the slots are where the specification puts them
        self.assertEqual(buf[0:8], bytes([7, 0, 0, 0, 3, 0, 0xD2, 0x0F]))       # page_id 7, 3 slots, 4050
        self.assertEqual(buf[8:12], bytes([0xF3, 0x0F, 13, 0]))                 # slot 0: (4083, 13)
        self.assertEqual(buf[4083:4096], b'a' * 13)

    def run_example(self, policy):
        """Section 5.3: B = 3, trace 1 2 3 1 4 2, each page unpinned at once."""
        disk = DiskManager(self.dir)
        self.addCleanup(disk.close)
        for _ in range(6):
            disk.allocate_page('t.db')
        pool = BufferPool(disk, 3, policy)
        steps = []
        for p in [1, 2, 3, 1, 4, 2]:
            before = pool.stats()
            pool.fetch_page('t.db', p)
            pool.unpin_page('t.db', p)
            after = pool.stats()
            what = 'hit' if after['hits'] > before['hits'] else 'miss'
            if after['evictions'] > before['evictions']:
                what += ', evict'
            steps.append((what, [f.page_id for f in pool.frames if f is not None]))
        return pool, steps

    def test_the_buffer_example_reproduces_exactly_lru(self):
        pool, steps = self.run_example('lru')
        self.assertEqual([s[0] for s in steps], ['miss', 'miss', 'miss', 'hit', 'miss, evict', 'miss, evict'])
        self.assertEqual(pool.stats(), {'hits': 1, 'misses': 5, 'evictions': 2, 'reads': 5, 'writes': 0})
        by_age = [f.page_id for f in sorted(pool.frames, key=lambda f: f.last_used)]
        self.assertEqual(by_age, [1, 4, 2])                             # least to most recently used
        self.assertEqual(sorted(steps[4][1]), [1, 3, 4])                # 2 was evicted for 4
        self.assertEqual(sorted(steps[5][1]), [1, 2, 4])                # 3 was evicted for 2

    def test_the_buffer_example_reproduces_exactly_clock(self):
        pool, steps = self.run_example('clock')
        self.assertEqual([s[0] for s in steps], ['miss', 'miss', 'miss', 'hit', 'miss, evict', 'hit'])
        self.assertEqual(pool.stats(), {'hits': 2, 'misses': 4, 'evictions': 1, 'reads': 4, 'writes': 0})
        self.assertEqual(steps[4][1], [4, 2, 3])                        # the sweep cleared 1, 2, 3 and took 1
        self.assertEqual([f.referenced for f in pool.frames], [True, True, False])
        self.assertEqual(pool.hand, 1)                                  # just past the page it loaded

    def test_a_scan_of_a_table_twenty_times_larger_than_b_never_pins_more_than_two_pages(self):
        db = Database(self.dir, frames=8)
        heap = db.create_table('R', INTS)
        for i in range(255 * 40):
            heap.insert((i, i, i))
        self.assertEqual(heap.info.num_pages, 40)                       # 20 x B for B = 2
        db.close()
        db = Database(self.dir, frames=2)
        heap = db.table('R')
        most, count = 0, 0
        for _, row in heap.scan():
            most = max(most, db.pool.pinned)
            count += 1
        self.assertEqual((count, most), (255 * 40, 2))                  # a directory page and a data page
        self.assertEqual(db.pool.high_water, 2)
        self.assertEqual(db.pool.pinned, 0)
        scan = heap.scan()
        next(scan)
        scan.close()                                                    # stopping early still unpins both
        self.assertEqual(db.pool.pinned, 0)
        db.close()

    def test_the_same_command_twice_gives_identical_counters(self):
        db = Database(self.dir, frames=8)
        heap = db.create_table('R', R)
        for i in range(5000):
            heap.insert((i, f'name{i % 97}', i))
        db.close()
        results = []
        for _ in range(2):
            db = Database(self.dir, frames=3, policy='clock')
            rows = [row for _, row in db.table('R').scan()]
            results.append((len(rows), db.pool.stats()))
            db.disk.close()
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 5000)

    def test_deleting_every_record_and_scanning_returns_nothing(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('R', R)
        rids = [heap.insert((i, 'x', i)) for i in range(1000)]
        for rid in rids:
            heap.delete(rid)
        self.assertEqual(list(heap.scan()), [])
        self.assertEqual(heap.info.num_tuples, 0)
        db.close()
        db = Database(self.dir, frames=2)
        self.assertEqual(list(db.table('R').scan()), [])                # and after reopening
        self.assertEqual(db.catalog.get_table('R').num_tuples, 0)
        db.close()


class RecordTests(unittest.TestCase):

    def test_the_example_of_the_specification(self):
        codec = RecordCodec([('INT', None), ('VARCHAR', 5), ('INT', None)])
        data = codec.encode((42, 'bob', 7))
        self.assertEqual(data.hex(' ').upper(), '2A 00 00 00 03 00 62 6F 62 07 00 00 00')
        self.assertEqual(len(data), 13)
        self.assertEqual(codec.decode(data), (42, 'bob', 7))

    def test_round_trip_and_size(self):
        codec = RecordCodec([('INT', None), ('VARCHAR', 20), ('INT', None)])
        for row in [(7, 'bob', 42), (-2 ** 31, '', 2 ** 31 - 1), (0, 'héllo wörld', -1)]:
            data = codec.encode(row)
            self.assertEqual(codec.decode(data), row)
            self.assertEqual(codec.size(row), len(data))
        self.assertEqual(RecordCodec([('INT', None)] * 3).size((1, 2, 3)), 12)
        self.assertEqual(codec.encode((-1, 'é', 0))[4:8], bytes([2, 0, 0xC3, 0xA9]))     # L counts bytes

    def test_the_wide_int_of_temporary_files(self):
        """(INT, 8) is the 8-byte integer a SUM is given in a temporary file. A table's INT is
        (INT, None) and stays 4 bytes: the format of the specification is not touched."""
        table = RecordCodec([('INT', None), ('VARCHAR', 5)])
        wide = RecordCodec([('INT', 8), ('VARCHAR', 5)])
        self.assertEqual((len(table.encode((7, 'ab'))), len(wide.encode((7, 'ab')))), (8, 12))
        for codec in (wide, RecordCodec([('INT', None), ('INT', 8)])):
            row = (6_000_000_000, 'ab') if codec is wide else (-5, -6_000_000_000)
            self.assertEqual(codec.decode(codec.encode(row)), row)
            self.assertEqual(codec.size(row), len(codec.encode(row)))
        for codec, row in ((table, (6_000_000_000, 'ab')), (RecordCodec([('INT', None)]), (2 ** 31,)),
                           (wide, (2 ** 63, 'ab'))):
            with self.assertRaises(RecordError):                # a clean error, never a struct.error
                codec.encode(row)

    def test_rows_that_do_not_fit_the_schema(self):
        codec = RecordCodec([('INT', None), ('VARCHAR', 3)])
        for row in [(1,), ('x', 'y'), (2 ** 31, 'y'), (1, 'four'), (1, 'ééé'), (1, 5), (True, 'y')]:
            with self.subTest(row=row):
                with self.assertRaises(RecordError):
                    codec.check(row)


class PageTests(unittest.TestCase):

    def setUp(self):
        self.buf = bytearray(PAGE_SIZE)
        page.init(self.buf, 1)

    def test_capacity_of_fixed_length_records(self):
        for length, expected in [(13, 240), (12, 255), (210, 19)]:
            page.init(self.buf, 1)
            count = 0
            while page.insert(self.buf, bytes(length)) is not None:
                count += 1
            self.assertEqual(count, 4088 // (length + 4))
            self.assertEqual(count, expected)
            self.assertLess(page.free_space(self.buf), length + 4)

    def test_no_space_leaves_the_page_unchanged(self):
        page.insert(self.buf, bytes(4000))
        before = bytes(self.buf)
        self.assertIsNone(page.insert(self.buf, bytes(85)))             # needs 89, 84 are free
        self.assertEqual(bytes(self.buf), before)
        self.assertEqual(page.insert(self.buf, bytes(80)), 1)
        page.init(self.buf, 1)
        self.assertEqual(page.insert(self.buf, bytes(4084)), 0)         # the longest record there is
        page.init(self.buf, 1)
        self.assertIsNone(page.insert(self.buf, bytes(4085)))

    def test_slot_numbers_are_never_reused(self):
        for i in range(5):
            page.insert(self.buf, bytes([i]) * 10)
        page.delete(self.buf, 2)
        self.assertEqual(page.insert(self.buf, b'x' * 10), 5)           # a new slot, not slot 2
        self.assertEqual([i for i, _ in page.records(self.buf)], [0, 1, 3, 4, 5])
        self.assertEqual(page.get(self.buf, 3), bytes([3]) * 10)        # its neighbours did not move

    def test_errors(self):
        page.insert(self.buf, b'abc')
        with self.assertRaises(page.PageError):
            page.get(self.buf, 1)                                       # slot_no >= slot_count
        page.delete(self.buf, 0)
        with self.assertRaises(page.PageError) as cm:
            page.get(self.buf, 0)                                       # dead
        self.assertIn('dead', str(cm.exception))
        with self.assertRaises(page.PageError):
            page.delete(self.buf, 0)                                    # already dead
        with self.assertRaises(page.PageError):
            page.delete(self.buf, 9)


class BufferPoolTests(TempDirCase):

    def pool(self, frames, policy='lru', pages=100, file='t.db'):
        disk = DiskManager(self.dir)
        for _ in range(pages):
            disk.allocate_page(file)
        self.addCleanup(disk.close)
        return BufferPool(disk, frames, policy)

    def touch(self, pool, page_ids, file='t.db'):
        for p in page_ids:
            pool.fetch_page(file, p)
            pool.unpin_page(file, p)

    def resident(self, pool):
        return sorted(p for _, p in pool.page_table)

    def test_the_first_b_misses_are_not_evictions(self):
        for policy in ('lru', 'clock'):
            pool = BufferPool(self.pool(1).disk, 16, policy)
            self.touch(pool, range(16))
            self.assertEqual(pool.stats(), {'hits': 0, 'misses': 16, 'evictions': 0, 'reads': 16, 'writes': 0})
            self.touch(pool, range(16, 20))
            self.assertEqual(pool.stats()['evictions'], 4)
            self.assertEqual(pool.stats()['misses'], pool.stats()['reads'])

    def test_fetch_fails_when_every_frame_is_pinned(self):
        for policy in ('lru', 'clock'):
            with self.subTest(policy=policy):
                pool = BufferPool(self.pool(1).disk, 3, policy)
                for p in (0, 1, 2):
                    pool.fetch_page('t.db', p)                          # all three frames pinned
                with self.assertRaises(BufferPoolError) as cm:
                    pool.fetch_page('t.db', 3)
                self.assertIn('no unpinned frame available', str(cm.exception))
                self.assertEqual(self.resident(pool), [0, 1, 2])        # nothing was evicted
                pool.unpin_page('t.db', 1)
                pool.fetch_page('t.db', 3)                              # page 1 is the only candidate
                self.assertEqual(self.resident(pool), [0, 2, 3])

    def test_unpinning_at_pin_count_zero_is_an_error(self):
        pool = self.pool(2)
        with self.assertRaises(BufferPoolError):
            pool.unpin_page('t.db', 0)                                  # not resident
        pool.fetch_page('t.db', 0)
        pool.fetch_page('t.db', 0)                                      # pinned twice
        pool.unpin_page('t.db', 0)
        pool.unpin_page('t.db', 0)
        with self.assertRaises(BufferPoolError) as cm:
            pool.unpin_page('t.db', 0)
        self.assertIn('already 0', str(cm.exception))

    def test_a_hit_makes_the_page_most_recently_used_under_lru(self):
        pool = self.pool(3)
        self.touch(pool, [0, 1, 2, 0, 3])                               # 1 is the oldest when 3 arrives
        self.assertEqual(self.resident(pool), [0, 2, 3])

    def test_clock_skips_pinned_frames_and_gives_a_second_chance(self):
        pool = self.pool(3, 'clock')
        pool.fetch_page('t.db', 0)                                      # stays pinned
        self.touch(pool, [1, 2, 3])                                     # hand: skip 0, clear 1 and 2, take 1
        self.assertEqual([f.page_id for f in pool.frames], [0, 3, 2])
        self.touch(pool, [4])                                           # 2's bit is clear: it goes
        self.assertEqual([f.page_id for f in pool.frames], [0, 3, 4])

    def test_the_policies_differ_on_the_same_trace(self):
        trace = [1, 2, 3, 1, 4, 2]
        self.assertEqual(run_trace(trace, 3, 'lru')['hits'], 1)
        self.assertEqual(run_trace(trace, 3, 'clock')['hits'], 2)

    def test_a_repeated_scan_of_a_file_larger_than_the_pool_never_hits(self):
        trace = list(range(80)) * 10                                    # trace 3 of the specification
        for policy in ('lru', 'clock'):
            for frames in (4, 16, 64):
                with self.subTest(policy=policy, frames=frames):
                    stats = run_trace(trace, frames, policy)
                    self.assertEqual((stats['hits'], stats['misses']), (0, 800))
                    self.assertEqual(stats['evictions'], 800 - frames)
            self.assertEqual(run_trace(trace, 256, policy)['hits'], 720)        # the whole file fits

    def test_dirty_pages_are_written_on_eviction_and_on_flush(self):
        pool = self.pool(2)
        frame = pool.fetch_page('t.db', 0)
        frame.data[0:5] = b'hello'
        pool.unpin_page('t.db', 0, True)
        self.touch(pool, [1, 2])                                        # evicts page 0, which is dirty
        self.assertEqual(pool.stats()['writes'], 1)
        self.touch(pool, [3])                                           # evicts a clean page: no write
        self.assertEqual(pool.stats()['writes'], 1)
        self.assertEqual(bytes(pool.fetch_page('t.db', 0).data[0:5]), b'hello')
        pool.unpin_page('t.db', 0)
        frame = pool.fetch_page('t.db', 1)
        frame.data[0:3] = b'abc'
        pool.unpin_page('t.db', 1, True)
        pool.flush_all()
        self.assertEqual(pool.stats()['writes'], 2)
        pool.flush_all()                                                # the flag was cleared
        self.assertEqual(pool.stats()['writes'], 2)
        self.assertEqual(pool.disk.read_page('t.db', 1)[0:3], b'abc')

    def test_new_page_is_zero_filled_pinned_and_dirty(self):
        pool = self.pool(2, pages=3)
        frame = pool.new_page('t.db')
        self.assertEqual((frame.page_id, bytes(frame.data), frame.pin_count, frame.dirty),
                         (3, bytes(PAGE_SIZE), 1, True))
        self.assertEqual(pool.stats()['misses'], 0)                     # no disk read

    def test_the_same_page_id_in_two_files_is_two_pages(self):
        pool = self.pool(4)
        for _ in range(3):
            pool.disk.allocate_page('u.db')
        a = pool.fetch_page('t.db', 2)
        b = pool.fetch_page('u.db', 2)
        self.assertIsNot(a, b)
        a.data[0] = 1
        self.assertEqual(b.data[0], 0)
        self.assertEqual(pool.stats()['misses'], 2)

    def test_reserved_frames_cannot_be_taken(self):
        pool = self.pool(4)
        self.touch(pool, [0, 1, 2, 3])
        pool.reserve(3)                                                 # evicts three unpinned pages
        self.assertEqual(len(pool.page_table), 1)
        pool.fetch_page('t.db', 5)
        with self.assertRaises(BufferPoolError):
            pool.fetch_page('t.db', 6)                                  # three reserved, one pinned
        with self.assertRaises(BufferPoolError):
            pool.reserve(1)
        self.assertEqual((pool.frames_in_use(), pool.high_water), (4, 4))
        pool.release(3)
        pool.fetch_page('t.db', 6)


class HeapTests(TempDirCase):

    def test_one_file_per_table_with_a_directory_in_page_0(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('R', R)
        self.assertEqual(db.disk.num_pages('R.db'), 1)                  # just the directory page
        rid = heap.insert((42, 'bob', 7))
        self.assertEqual(rid, (1, 0))
        db.close()
        self.assertEqual(sorted(os.listdir(self.dir)), ['R.db', 'catalog.db'])
        self.assertEqual(os.path.getsize(os.path.join(self.dir, 'R.db')), 2 * PAGE_SIZE)
        with open(os.path.join(self.dir, 'R.db'), 'rb') as f:
            directory, data = f.read(PAGE_SIZE), f.read(PAGE_SIZE)
        self.assertEqual(DIR_HEADER.unpack_from(directory, 0), (0, 0xFFFFFFFF, 1))
        self.assertEqual(entry_at(directory, 0), (1, 4088 - 17))        # data page 1, free bytes
        self.assertEqual(directory[10:16], bytes([1, 0, 0, 0, 0xE7, 0x0F]))
        self.assertEqual(page.header(data), (1, 1, 4083))
        self.assertEqual(data[4083:], bytes.fromhex('2A000000 0300 626F62 07000000'))

    def test_insert_get_delete_scan(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('R', R)
        rids = [heap.insert((i, f'name{i % 7}', i * 2)) for i in range(2000)]
        self.assertEqual(heap.get(rids[1234]), (1234, 'name2', 2468))
        self.assertGreater(heap.info.num_pages, 5)
        for rid in rids[::2]:
            heap.delete(rid)
        self.assertEqual([row[0] for _, row in heap.scan()], list(range(1, 2000, 2)))
        self.assertEqual([rid for rid, _ in heap.scan()], rids[1::2])   # rids did not change
        self.assertEqual(heap.info.num_tuples, 1000)
        db.close()

    def test_reading_or_deleting_a_dead_slot_names_the_rid(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('R', R)
        rid = heap.insert((1, 'a', 2))
        heap.delete(rid)
        for action in (heap.get, heap.delete):
            with self.assertRaises(RecordError) as cm:
                action(rid)
            self.assertIn('rid (1, 0)', str(cm.exception))
            self.assertIn('dead', str(cm.exception))
        for bad in [(1, 5), (0, 0), (9, 0)]:                            # no such slot, a directory page, no such page
            with self.assertRaises(RecordError):
                heap.get(bad)
        self.assertEqual(db.pool.pinned, 0)                             # errors do not leave pins behind
        db.close()

    def test_insert_is_first_fit_over_the_directory(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('V', [('a', 'INT', None), ('s', 'VARCHAR', 255)])
        long_rows = [heap.insert((i, 'x' * 255)) for i in range(40)]    # 265 bytes with the slot: 15 a page
        self.assertEqual([rid[0] for rid in long_rows[:16]], [1] * 15 + [2])
        self.assertEqual(heap.info.num_pages, 3)
        # Page 1 has 4088 - 15 * 265 = 113 bytes free: no room for a long row, room for a short one.
        self.assertEqual(heap.insert((99, 'short')), (1, 15))           # 11 bytes + slot: 98 left
        self.assertEqual(heap.insert((100, 'x' * 255))[0], 3)           # the long one goes to the last page
        self.assertEqual(heap.insert((101, 'y' * 80)), (1, 16))         # 86 bytes + slot = 90: fits page 1
        self.assertEqual(heap.insert((102, 'z' * 80)), (2, 15))         # page 1 has 8 left: page 2 is next
        self.assertEqual(heap.insert((103, 'w' * 120))[0], 3)           # 130 bytes: neither 1 nor 2
        db.close()

    def test_deleted_space_is_not_reclaimed(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('I', INTS)
        rids = [heap.insert((i, i, i)) for i in range(255)]             # exactly one page
        self.assertEqual(heap.info.num_pages, 1)
        for rid in rids:
            heap.delete(rid)
        self.assertEqual(heap.insert((1, 2, 3)), (2, 0))                # page 1 is all dead slots: a new page
        self.assertEqual([row for _, row in heap.scan()], [(1, 2, 3)])
        db.close()

    def test_a_second_directory_page_after_681_data_pages(self):
        db = Database(self.dir, frames=8)
        heap = db.create_table('W', [('k', 'INT', None), ('pad', 'VARCHAR', 200)])
        rows = 19 * (DIR_CAPACITY + 2) + 1                              # 684 data pages
        for i in range(rows):
            heap.insert((i, 'p' * 200))
        self.assertEqual(heap.info.num_pages, DIR_CAPACITY + 3)
        db.close()
        db = Database(self.dir, frames=3)
        heap = db.table('W')
        file_pages = db.disk.num_pages('W.db')
        self.assertEqual(file_pages, 2 + DIR_CAPACITY + 3)              # two directory pages
        listed, dir_id = [], 0
        while dir_id != page.NO_PAGE:                                   # what the directory says
            frame = db.pool.fetch_page('W.db', dir_id)
            page_id, next_dir, count = DIR_HEADER.unpack_from(frame.data, 0)
            self.assertEqual(page_id, dir_id)
            listed += [entry_at(frame.data, i)[0] for i in range(count)]
            db.pool.unpin_page('W.db', dir_id)
            dir_id = next_dir
        self.assertEqual(listed, data_page_ids(file_pages))             # what the layout rule says
        self.assertTrue(is_directory_page(682))
        self.assertNotIn(682, listed)
        self.assertEqual([row[0] for _, row in heap.scan()], list(range(rows)))
        self.assertEqual(heap.get((683, 0))[0], 19 * DIR_CAPACITY)      # first row after the second directory
        self.assertEqual(db.pool.high_water, 2)
        db.close()

    def test_a_record_longer_than_a_page_names_the_table_and_the_row(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('Wide', [(f'c{i}', 'VARCHAR', 255) for i in range(17)])
        heap.insert(tuple('x' * 100 for _ in range(17)))                # 1,734 bytes: fine
        with self.assertRaises(RecordError) as cm:
            heap.insert(tuple('x' * 255 for _ in range(17)))            # 17 * 257 = 4,369 bytes
        self.assertIn('table Wide, row 2', str(cm.exception))
        self.assertIn('4369 bytes', str(cm.exception))
        db.close()

    def test_everything_survives_closing_and_reopening(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('R', R)
        for i in range(700):
            heap.insert((i, 'v', i))
        db.create_table('Other', [('k', 'INT', None)]).insert((5,))
        db.close()
        db = Database(self.dir, frames=4)
        self.assertEqual(db.catalog.list_tables(), ['Other', 'R'])
        info = db.catalog.get_table('R')
        self.assertEqual((info.columns, info.file, info.num_tuples), (R, 'R.db', 700))
        self.assertEqual(info.num_pages, db.disk.num_pages('R.db') - 1)
        self.assertEqual(sum(1 for _ in db.table('R').scan()), 700)
        self.assertEqual([row for _, row in db.table('Other').scan()], [(5,)])
        heap = db.table('R')
        self.assertEqual(heap.insert((700, 'v', 700))[0], info.num_pages)       # continues in the last page
        db.close()


class CatalogTests(TempDirCase):

    def test_an_entry_grows_over_several_pages_and_shrinks_again(self):
        db = Database(self.dir, frames=4)
        db.create_table('A', R)
        db.create_table('B', INTS)
        a = db.catalog.get_table('A')
        self.assertEqual((a.catalog_pages, db.catalog.get_table('B').catalog_pages), ([0], [1]))
        a.stats = {'histogram': list(range(3000))}                      # about 14,900 bytes of statistics
        db.catalog.save(a)
        self.assertEqual(a.catalog_pages, [0, 2, 3, 4])
        db.close()
        db = Database(self.dir, frames=2)
        a = db.catalog.get_table('A')
        self.assertEqual(a.stats, {'histogram': list(range(3000))})
        self.assertEqual(db.catalog.get_table('B').columns, INTS)
        a.stats = None
        db.catalog.save(a)                                              # back to one page
        self.assertEqual(a.catalog_pages, [0])
        db.create_table('C', INTS)                                      # takes a page that was handed back
        self.assertEqual(db.catalog.get_table('C').catalog_pages, [2])
        self.assertEqual(db.disk.num_pages('catalog.db'), 5)
        db.close()
        db = Database(self.dir)
        self.assertEqual(db.catalog.list_tables(), ['A', 'B', 'C'])
        db.close()

    def test_saving_again_and_again_uses_no_more_space(self):
        db = Database(self.dir, frames=4)
        heap = db.create_table('R', R)
        for i in range(50):
            heap.insert((i, 'x', i))
            heap.flush()                                                # rewrites the entry every time
        self.assertEqual(db.disk.num_pages('catalog.db'), 1)
        db.close()

    def test_errors(self):
        db = Database(self.dir, frames=4)
        db.create_table('R', R)
        open(os.path.join(self.dir, 'Stray.db'), 'wb').close()
        for name, columns in [('R', R), ('E', []), ('D', [('a', 'INT', None), ('a', 'INT', None)]),
                              ('V', [('a', 'VARCHAR', 300)]), ('catalog', R), ('bad name', R), ('9x', R),
                              ('Stray', R)]:
            with self.subTest(table=name):
                with self.assertRaises(CatalogError):
                    db.create_table(name, columns)
        with self.assertRaises(CatalogError):
            db.table('Missing')
        with self.assertRaises(RecordError) as cm:
            db.table('R').insert((1, 'a string longer than twenty bytes', 2))
        self.assertIn('column b is VARCHAR(20)', str(cm.exception))
        db.close()


class LoaderTests(TempDirCase):

    def load(self, text, columns=R):
        db = Database(os.path.join(self.dir, 'db'))
        heap = db.create_table('R', columns)
        path = os.path.join(self.dir, 'r.csv')
        with open(path, 'w', encoding='utf8', newline='') as f:
            f.write(text)
        try:
            return load_csv(heap, path), heap
        finally:
            self.rows = [row for _, row in heap.scan()]
            db.close()

    def test_no_header_line_and_quoted_commas(self):
        count, heap = self.load('1,bob,2\n2,"Smith, Jane",3\n3,"say ""hi""",4\n\n4,,5\n')
        self.assertEqual(count, 4)
        self.assertEqual(self.rows, [(1, 'bob', 2), (2, 'Smith, Jane', 3), (3, 'say "hi"', 4), (4, '', 5)])

    def test_the_first_line_is_data(self):
        with self.assertRaises(RecordError) as cm:
            self.load('a,b,c\n1,x,2\n')
        self.assertIn("line 1, column a: 'a' is not an integer", str(cm.exception))

    def test_a_bad_field_names_the_line_and_the_column_and_stops_the_load(self):
        for text, expected in [('1,a,2\n2,b,x\n3,c,4\n', "line 2, column c: 'x' is not an integer"),
                               ('1,a,2\n2,' + 'b' * 21 + ',3\n', 'line 2, column b: the value is 21 bytes long'),
                               ('1,a,2\n2,b\n', 'line 2: 2 fields, but table R has 3 columns'),
                               ('1,a,2\n99999999999,b,3\n', 'line 2, column a: 99999999999 does not fit')]:
            with self.subTest(expected=expected):
                with self.assertRaises(RecordError) as cm:
                    self.load(text)
                self.assertIn(expected, str(cm.exception))
                self.assertEqual(self.rows, [(1, 'a', 2)])              # line 1 stays, nothing after
                self.tearDown()
                self.setUp()

    def test_schema_text(self):
        self.assertEqual(parse_schema('a:INT, b:VARCHAR(20),c:int'), R)
        for bad in ['a', 'a:FLOAT', 'a:VARCHAR', ':INT']:
            with self.assertRaises(CatalogError):
                parse_schema(bad)


class CommandLineTests(TempDirCase):

    def storage(self, *args):
        return subprocess.run([sys.executable, 'storage.py', *args], cwd=ROOT, capture_output=True, text=True)

    def make(self, rows):
        db = os.path.join(self.dir, 'db')
        csv_path = os.path.join(self.dir, 'r.csv')
        with open(csv_path, 'w') as f:
            f.write(rows)
        r = self.storage('create-table', '--db', db, '--name', 'R', '--schema', 'a:INT,b:VARCHAR(20),c:INT')
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.storage('load', '--db', db, '--table', 'R', '--file', csv_path)
        self.assertEqual(r.returncode, 0, r.stdout)
        return db, r.stdout

    def test_scan_json_has_the_required_shape(self):
        db, out = self.make('42,bob,7\n43,amy,9\n44,"a, b",1\n')
        self.assertIn('loaded 3 rows', out)
        r = self.storage('scan', '--db', db, '--table', 'R', '--frames', '8', '--json')
        result = json.loads(r.stdout)
        self.assertEqual(result['table'], 'R')
        self.assertEqual(result['schema'], [['a', 'INT'], ['b', 'VARCHAR', 20], ['c', 'INT']])
        self.assertEqual(result['rows'], [[42, 'bob', 7], [43, 'amy', 9], [44, 'a, b', 1]])
        self.assertEqual(set(result['stats']), {'hits', 'misses', 'evictions', 'reads', 'writes'})
        self.assertEqual(result['stats']['misses'], result['stats']['reads'])
        self.assertEqual(result['stats']['writes'], 0)
        limited = json.loads(self.storage('scan', '--db', db, '--table', 'R', '--frames', '2',
                                          '--limit', '2', '--json').stdout)
        self.assertEqual(limited['rows'], [[42, 'bob', 7], [43, 'amy', 9]])
        again = self.storage('scan', '--db', db, '--table', 'R', '--frames', '8', '--json')
        self.assertEqual(again.stdout, r.stdout)                        # the same command twice

    def test_scan_writes_each_row_as_it_reads_it(self):
        """The command keeps no list of rows. When the first row is written, the catalog page, the
        directory page and one data page have been read, and the other four data pages have not."""
        db, _ = self.make(''.join(f'{i},bob,{i}\n' for i in range(1000)))      # 240 rows a page: 5 data pages
        opened = []
        real_open = storage.open_database

        def remember(a, must_exist=True):
            opened.append(real_open(a, must_exist))
            return opened[-1]

        class Out:
            text = ''
            reads_at_first_row = None

            def write(self, s):
                self.text += s
                if self.reads_at_first_row is None and '"rows": [[' in self.text:
                    self.reads_at_first_row = opened[0].pool.stats()['reads']

        out = Out()
        with mock.patch.object(storage, 'open_database', remember), mock.patch.object(sys, 'stdout', out):
            self.assertEqual(storage.main(['scan', '--db', db, '--table', 'R', '--frames', '2', '--json']), 0)
        result = json.loads(out.text)
        self.assertEqual(len(result['rows']), 1000)
        self.assertEqual(result['stats']['reads'], 7)
        self.assertEqual(out.reads_at_first_row, 3)

    def test_layout_reproduces_the_page_example(self):
        """Records of 13, 20 and 13 bytes, then slot 1 deleted: Section 3.2 through the tool."""
        db, _ = self.make('42,bob,7\n1,abcdefghij,2\n43,amy,9\n')
        r = self.storage('layout', '--db', db, '--table', 'R', '--page', '1')
        self.assertIn('page_id=1 slot_count=3 free_end=4050 free_space=4030', r.stdout)
        self.assertIn('slot directory: 0:(4083,13) 1:(4063,20) 2:(4050,13)', r.stdout)
        self.assertIn("slot 0 at byte 8: offset=4083 length=13  (42, 'bob', 7)", r.stdout)
        handle = Database(db)
        handle.table('R').delete((1, 1))
        handle.close()
        r = self.storage('layout', '--db', db, '--table', 'R', '--page', '1')
        self.assertIn('page_id=1 slot_count=3 free_end=4050 free_space=4030', r.stdout)     # nothing freed
        self.assertIn('slot directory: 0:(4083,13) 1:(0,0) 2:(4050,13)', r.stdout)
        self.assertIn('slot 1 at byte 12: dead', r.stdout)
        r = self.storage('layout', '--db', db, '--table', 'R', '--page', '0')
        self.assertIn('directory page', r.stdout)
        self.assertIn('page_id=0 next_dir_page=none entry_count=1', r.stdout)
        self.assertIn('entry 0 at byte 10: data_page_id=1 free_bytes=4030', r.stdout)
        self.assertIn('R.db has pages 0 to 1', self.storage('layout', '--db', db, '--table', 'R', '--page', '2').stdout)

    def test_trace_reproduces_the_buffer_example(self):
        path = os.path.join(self.dir, 'trace.txt')
        with open(path, 'w') as f:
            f.write('1\n2\n3\n1\n4\n2\n')
        lru = json.loads(self.storage('trace', '--db', self.dir, '--file', path, '--frames', '3',
                                      '--policy', 'lru', '--json').stdout)
        self.assertEqual({k: lru[k] for k in ('policy', 'frames', 'hits', 'misses', 'evictions', 'reads', 'writes')},
                         {'policy': 'lru', 'frames': 3, 'hits': 1, 'misses': 5, 'evictions': 2, 'reads': 5,
                          'writes': 0})
        clock = json.loads(self.storage('trace', '--db', self.dir, '--file', path, '--frames', '3',
                                        '--policy', 'clock', '--json').stdout)
        self.assertEqual((clock['hits'], clock['misses'], clock['evictions'], clock['reads'], clock['writes']),
                         (2, 4, 1, 4, 0))

    def test_the_trace_files_give_the_numbers_of_the_report(self):
        """traces/ holds my five traces and the worked trace of Section 5.3. The command is run
        from the project root on the files themselves, the way REPORT_M1.md says to."""
        def run(name, frames, policy):
            out = json.loads(self.storage('trace', '--file', os.path.join('traces', name), '--frames', str(frames),
                                          '--policy', policy, '--json').stdout)
            return out['hits'], out['misses'], out['evictions']

        self.assertEqual(run('example.txt', 3, 'lru'), (1, 5, 2))
        self.assertEqual(run('example.txt', 3, 'clock'), (2, 4, 1))
        self.assertEqual(run('trace3.txt', 64, 'lru'), (0, 800, 736))       # the repeated scan: never a hit
        self.assertEqual(run('trace3.txt', 64, 'clock'), (0, 800, 736))
        self.assertEqual(run('trace3.txt', 256, 'lru'), (720, 80, 0))       # the file fits: only the first pass misses
        self.assertEqual(run('trace2.txt', 64, 'lru'), (2998, 2002, 1938))  # the row quoted in the report
        self.assertEqual(run('trace2.txt', 64, 'clock')[0], 2870)

    def test_errors_are_one_line(self):
        db, _ = self.make('1,a,2\n')
        bad = os.path.join(self.dir, 'bad.csv')
        with open(bad, 'w') as f:
            f.write('2,b,3\n3,c,oops\n')
        trace = os.path.join(self.dir, 'trace.txt')
        with open(trace, 'w') as f:
            f.write('1\nx\n')
        for args, expected in [
                (('load', '--db', db, '--table', 'R', '--file', bad), "error: line 2, column c: 'oops' is not an integer"),
                (('scan', '--db', db, '--table', 'Nope'), "error: unknown table 'Nope'"),
                (('scan', '--db', os.path.join(self.dir, 'none'), '--table', 'R'), 'error: there is no database in'),
                (('create-table', '--db', db, '--name', 'R', '--schema', 'a:INT'), "error: table 'R' already exists"),
                (('trace', '--file', trace, '--frames', '3'), "line 2: 'x' is not a page id"),
                (('trace', '--file', trace, '--frames', '0'), 'error:')]:
            with self.subTest(args=args[0], expected=expected):
                r = self.storage(*args)
                self.assertEqual(r.returncode, 1)
                self.assertIn(expected, r.stdout)
                self.assertEqual(len(r.stdout.strip().splitlines()), 1)
                self.assertNotIn('Traceback', r.stderr)
        r = self.storage('scan', '--db', db, '--table', 'R', '--json')
        self.assertEqual(json.loads(r.stdout)['rows'], [[1, 'a', 2], [2, 'b', 3]])     # line 1 of bad.csv stayed
