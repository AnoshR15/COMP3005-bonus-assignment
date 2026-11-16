# DESIGN_M1.md — Storage engine and buffer pool

Everything above this module asks it for a page. It is the only code that touches a file.

| File | Contents |
|---|---|
| `storage.py` | command line: create-table, load, scan, layout, trace, tables |
| `store/disk.py` | disk manager: page reads and writes on named files, counted |
| `store/record.py` | record format |
| `store/page.py` | slotted page |
| `store/heap.py` | heap file: directory pages, first fit, scan |
| `store/buffer.py` | buffer pool, LRU and CLOCK |
| `store/catalog.py` | the catalog and its page format |
| `store/db.py` | a database handle, schema text, the CSV loader |

Formats fixed by the specification are implemented as given and only summarised here. The parts
left to me are sections 4 (compaction), 5.2 (free space strategy) and 7 (catalog format).

**Assumption.** No trace files were published, so the five traces of REPORT_M1.md are my own,
in `traces/trace1.txt` to `trace5.txt`, one page id per line as in the worked trace of Section
5.3; trace 3 is the 80-page file scanned ten times that the specification describes.

## 1. Files and page addressing

A database is a directory holding `catalog.db` and one `<table>.db` per table. Each file is a
sequence of 4096-byte pages with nothing outside the pages. A page id is the index of a page in
its file, so a page is named by (file name, page id), and that pair is the key of the buffer
pool's page table. A record id is (page id, slot number) within the table's file. All integers
are little endian.

`DiskManager` opens each file once, counts every `read_page` and `write_page`, and hands out new
page ids at the end of a file. A table name must be letters, digits and underscores, because it
becomes a file name, and it may not be `catalog`.

Table names are case sensitive (`Emp` and `emp` are different tables, and a query against the
wrong one is an unknown-table error), but a table's file is `<table>.db` by specification, so
on a file system that ignores case, as Windows and macOS do by default, `Emp.db` and `emp.db`
are one file and the two tables cannot both exist. `create-table` refuses the second with
`a file named emp.db is already in the database directory` instead of sharing the file. On
Linux both are created.

## 2. Record format

Field encodings in schema order; no header, no null bitmap. INT is 4 bytes signed. VARCHAR(n) is
a 2-byte length L followed by L bytes of UTF-8, with L ≤ n ≤ 255. L counts bytes, not
characters: `'é'` has L = 2. The example of the specification, (42, 'bob', 7) under
(INT, VARCHAR(5), INT), encodes to `2A 00 00 00 03 00 62 6F 62 07 00 00 00`, 13 bytes; a test
compares the bytes. `RecordCodec` never assumes a fixed length. When a schema is all INT it
uses one precompiled `struct` as a shortcut, and that is the only place the distinction exists.

## 3. Slotted page

```
offset  size  field
     0     4  page_id      uint32
     4     2  slot_count   uint16   includes dead slots
     6     2  free_end     uint16   lowest byte used by record data; 4096 when empty
     8     4  slot 0       uint16 offset, uint16 length
    12     4  slot 1
   ...                     free space
 free_end                  record data, newest lowest
  4095
```

free space = free_end − (8 + 4 × slot_count). An insert of L bytes needs L + 4. A dead slot has
offset 0. Slot numbers are never reused. `store/page.py` is the three operations of the
specification and nothing else; `insert` returns `None` for NO_SPACE and leaves the page
untouched.

### A real page

Table `R (a INT, b VARCHAR(20), c INT)`. Three rows inserted, (42, 'bob', 7), (1, 'abcdefghij', 2),
(43, 'amy', 9), which are 13, 20 and 13 bytes, then the second deleted: the example of Section
3.2. These are the bytes of page 1 of `R.db` (`results/m1/page_dump.txt`, written by
`python -m bench.m1_measure dump`).

```
    0  01 00 00 00 03 00 d2 0f  f3 0f 0d 00 00 00 00 00
   16  d2 0f 0d 00 00 00 00 00  00 00 00 00 00 00 00 00
 ...   (zeros: free space, bytes 20 to 4049)
 4048  00 00 2b 00 00 00 03 00  61 6d 79 09 00 00 00 01
 4064  00 00 00 0a 00 61 62 63  64 65 66 67 68 69 6a 02
 4080  00 00 00 2a 00 00 00 03  00 62 6f 62 07 00 00 00
```

| Bytes | Hex | Meaning |
|---|---|---|
| 0–3 | `01 00 00 00` | page_id = 1 |
| 4–5 | `03 00` | slot_count = 3 |
| 6–7 | `d2 0f` | free_end = 0x0FD2 = 4050 |
| 8–11 | `f3 0f 0d 00` | slot 0: offset 4083, length 13 |
| 12–15 | `00 00 00 00` | slot 1: dead |
| 16–19 | `d2 0f 0d 00` | slot 2: offset 4050, length 13 |
| 20–4049 | zeros | free space: 4050 − (8 + 4 × 3) = 4030 bytes |
| 4050–4062 | `2b 00 00 00` `03 00` `61 6d 79` `09 00 00 00` | record of slot 2: 43, length 3, "amy", 9 |
| 4063–4082 | `01 00 00 00` `0a 00` `61 … 6a` `02 00 00 00` | the deleted record, 20 bytes, still there, referred to by no slot |
| 4083–4095 | `2a 00 00 00` `03 00` `62 6f 62` `07 00 00 00` | record of slot 0: 42, length 3, "bob", 7 |

`storage layout --table R --page 1` prints the same page as
`page_id=1 slot_count=3 free_end=4050 free_space=4030` and
`slot directory: 0:(4083,13) 1:(0,0) 2:(4050,13)`, the last row of the table in Section 3.2.
The 20 bytes at 4063 are the point of that row: deleting freed no usable space.

## 4. Compaction: not implemented

A deleted record's bytes and its slot stay in the page for good. The decision and its reasons:

- **Nothing in the system deletes.** The SQL subset has CREATE, INSERT and SELECT. `delete(rid)`
  exists because the contract requires it and is tested, but no statement reaches it. Compaction
  would be code that only its own tests run.
- **It would not give back the slot.** Slot numbers are never reused, so each deleted record
  costs 4 bytes of directory forever, compaction or not. A page from which many short records
  were deleted keeps a long tail of dead slots.
- **It complicates the one promise the free space directory relies on.** Without compaction a
  page's free space only ever shrinks. Section 5.2 uses exactly that to make first fit cheap.

The cost is visible and tested: fill a page with 255 rows, delete all 255, insert one row, and
the row goes to a new page. The first page is 4,080 bytes of dead records with 8 bytes free.

If deletes became real, I would compact a page inside `delete` when its dead bytes pass a
quarter of the page: slide the live records to the end, rewrite their slot offsets, raise
`free_end`, write the new free space to the directory, and forget the first-fit hint.

## 5. Heap file

### 5.1 Layout

Page 0 of a table file is a directory page; directory pages are linked.

```
offset  size  field
     0     4  page_id         uint32
     4     4  next_dir_page   uint32, 0xFFFFFFFF if none
     8     2  entry_count     uint16
    10     6  entry 0         uint32 data_page_id, uint16 free_bytes
    16     6  entry 1
   ...                        681 entries: floor((4096 - 10) / 6)
```

Page 0 of the `R.db` above is `00 00 00 00 | ff ff ff ff | 01 00 | 01 00 00 00 be 0f`:
page 0, no next directory page, one entry: data page 1 with 0x0FBE = 4030 bytes free.

A new table is one empty directory page. Data pages are added at the end of the file when
needed, and a new directory page is added exactly when the last one is full. The file is
therefore always directory, 681 data pages, directory, 681 data pages, …: **page p is a
directory page exactly when p is a multiple of 682**, and the data pages in directory order are
the remaining page ids in increasing order. A test builds a table of 684 data pages and compares
what the directories list with what this rule predicts.

### 5.2 Free space strategy: first fit, with a hint that keeps it exact

`insert` walks the directory from the front and takes the first entry with
`free_bytes ≥ L + 4`. It fetches that data page, inserts, writes the page's new free space into
the entry, and unpins both. If no entry fits it adds a data page (and a directory page first, if
the last one has 681 entries).

Done literally, that reads every directory entry for every row: loading a 500-page table would
look at about 30 million entries. So `HeapFile` remembers one fact in memory, the *hint*: a
record size `need` and a directory position, meaning "every entry before this position has less
than `need` bytes free." An insert that needs at least that much starts there. An insert that
needs less starts from the front, as the rule says, and leaves a new hint.

This is still first fit, entry for entry, not an approximation of it, and the reason is section
4: with no compaction a page's free space never grows, so an entry that was too small for `need`
bytes stays too small for anything at least that long. The hint is two integers, is lost when
the table is closed, and costs one walk from the front after reopening.

Why first fit and not best fit: one pass, stops early, and short records fill the gaps that long
ones left in earlier pages. A test shows it: after rows of 265 bytes leave 113 bytes free in
each page, a 15-byte row goes into page 1, not the last page.

### 5.3 The other operations

- `get(rid)`: fetch the data page, read the slot, unpin: one page. A dead slot, a slot that does
  not exist, a directory page and a page outside the file are errors naming the table and rid.
- `delete(rid)`: fetch the data page, mark the slot dead, unpin. Then fetch the directory page
  that lists it (page ⌊p / 682⌋ × 682, entry p mod 682 − 1) and bring `free_bytes` up to date.
  With no compaction the value is unchanged, so the directory page is not marked dirty.
- `scan()`: for each directory page, for each entry, fetch the data page, yield its live rows,
  unpin it, move on. **At most one directory page and one data page are pinned**, and both are
  unpinned when the caller stops early (a `try/finally` around each). A test scans a 40-page
  table with B = 2 and watches the pin count: it reaches 2 and never 3.

A record longer than an empty page can take (4,084 bytes, 4,088 minus its slot) is refused with
an error naming the table and the row number.

## 6. Buffer pool

A list of B frames and a page table from (file, page id) to frame index. `fetch_page`,
`unpin_page`, `new_page`, `flush_all` and `stats` follow the pseudo-code of the specification.

**Counting**, exactly: a hit is a fetch of a resident page; a miss is a fetch that read from
disk; an eviction is a resident page thrown out to make room, so the first B misses on an empty
pool are not evictions; a read is one `disk.read_page`; a write is one `disk.write_page`, on
evicting a dirty page or on flush. `new_page` reads nothing, so it is neither a hit nor a miss;
if it has to throw a page out, that is counted as an eviction.

**Errors.** If every frame is pinned, fetch fails with `no unpinned frame available`; nothing is
evicted. Unpinning a page that is not resident, or whose pin count is 0, is an error.

**LRU.** Every fetch stamps the frame with a counter that only goes up. The victim is the
unpinned frame with the smallest stamp.

**CLOCK.** A reference bit per frame and a hand. Every fetch sets the bit. When a page is loaded
into a frame the hand moves to the frame after it. To choose a victim the hand walks: pinned,
skip; bit set, clear it and move on; bit clear, that is the victim. After more than 2B steps
with no victim, every frame is pinned.

While the pool still has empty frames, a new page takes the lowest-numbered one and no victim is
chosen. In a trace that means frames 0, 1, 2, … in order with the hand following, which is what
the worked example shows.

Section 5.3 of the specification (B = 3, trace 1 2 3 1 4 2) is two tests and one command-line
test: LRU gives 1 hit, 5 misses, 2 evictions; CLOCK gives 2 hits, 4 misses, 1 eviction with the
pool ending as [4, 2, 3].

**One addition.** `reserve(n)` and `release(n)` take frames out of the pool for the execution
engine's working memory. A reserved frame is treated as pinned by both policies. Module 1's own
commands never call them.

## 7. The catalog and its page format

`catalog.db`, read and written through the buffer pool. One entry per table, a JSON object:

```json
{"name":"R","columns":[["a","INT",null],["b","VARCHAR",20],["c","INT",null]],
 "file":"R.db","pages":1,"tuples":2}
```

`pages` is the number of data pages, `tuples` the number of live rows. Module 4 adds a `stats`
member to the same object.

An entry is stored in one or more linked catalog pages:

```
offset  size  field
     0     4  page_id     uint32
     4     2  kind        uint16   0 unused, 1 first page of an entry, 2 continuation
     6     2  length      uint16   bytes of entry text in this page
     8     4  next_page   uint32   the entry's next page, 0xFFFFFFFF if none
    12   ...  entry text  UTF-8, up to 4084 bytes
```

Page 0 of the catalog of the database above begins
`00 00 00 00 | 01 00 | 70 00 | ff ff ff ff | 7b 22 6e 61 6d 65 …`: page 0, first page of an
entry, 0x70 = 112 bytes of text, no next page, then `{"name…`.

**Why not slotted pages.** That was my first design, and it breaks under this module's own
rules. A table's entry is rewritten whenever its counts change. In a slotted page that is a
delete and an insert, and a delete here frees nothing and its slot is never reused: every save
would leak the old entry, and the catalog would grow with every load. So an entry is rewritten
in place, in the pages it already has. If it has grown (statistics for many columns) it takes
unused pages or new ones; if it has shrunk, the pages it no longer needs are marked unused and
the next entry that needs a page takes them. A test saves one entry fifty times and checks the
catalog is still one page; another grows an entry to four pages, reads it back, and shrinks it.

**Why JSON for the text.** The entry has a variable number of columns now and nested statistics
later. A JSON library is allowed, and the page format around it is the part that had to be
designed.

Finding a table reads the catalog pages once per run; the entries are then kept in memory.

## 8. Loading

`load_csv`: no header line, one record per line, commas, double quotes around a field that
contains a comma (the standard library's `csv` reader). The first line is data. A field that
does not parse as an INT, an INT out of range, a VARCHAR longer in bytes than its column, or a
line with the wrong number of fields stops the load with `line N, column c: …`. Rows before that
line stay loaded and the catalog counts are written.

## 9. Command line

All commands take `--db DIR` (also spelled `--catalog DIR`); without it the database is the
directory `db`, so the commands run as the handout writes them. JSON shapes are those of the specification, with extra fields
only: `max_pinned` on scan, `requests` on trace.

- `scan`: writes each row as `HeapFile.scan` yields it and keeps none of them. The JSON object
  is written in pieces, `{"table", "schema", "rows": [` first, one row at a time, then `"stats"`,
  so the command's memory does not grow with the table: scanning the 500-page table of
  REPORT_M1.md section 3 peaks at 17 MB, the size of the interpreter, where a version that
  collected the rows in a list first peaked at 45 MB. A test checks that when the first row is
  written, one data page has been read and the other four have not.
- `layout --page P`: P is the page id in the table's file. A data page prints its header, free
  space and slot directory as `n:(offset,length)`; a directory page prints its entries.
- `trace`: fetches each page id and unpins it at once as clean. The page ids are not pages of
  any table, so the trace runs against a scratch file of zero pages as long as the largest id.
  `--db` is accepted and not needed.

## 10. How the execution engine uses this

Through `fetch_page`, `unpin_page`, the catalog and the disk manager's counters. One thing is
done differently on purpose. Module 2 requires a table scan to hold one frame and read M pages.
`HeapFile.scan` holds two, a directory page and a data page, and reads M + ⌈M / 681⌉. So the
executor's `seqscan` does not read the directory: it walks the page ids of the file and skips
the multiples of 682, which section 5.1 guarantees are exactly the directory pages. `storage
scan` follows the directory, as this module specifies.

## 11. Tests

`tests/test_store.py`. The first class is the checklist of Section 10 of the specification: the
page example, the buffer example under both policies, a scan of a table twenty times larger
than B that never pins more than two pages, identical counters from two runs, and a scan after
deleting every record. Then records, pages, the pool (including that fetching with every frame
pinned fails and that unpinning at zero fails), heap files, the catalog, the loader, and the
command line, where `layout` and `trace` reproduce both worked examples through the tool.
