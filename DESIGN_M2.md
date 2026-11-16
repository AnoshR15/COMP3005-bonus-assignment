# DESIGN_M2.md — Execution engine

A physical plan in JSON goes in; rows and counters come out. Every operator has the same four
methods, and the whole plan lives inside B buffer pool frames.

| File | Contents |
|---|---|
| `exec.py` | command line: `run`, `trace` |
| `executor/context.py` | counters, `Workspace`, `RunWriter`, `RunReader`, `partition`, the hash function |
| `executor/operators.py` | the iterator base class, the frame-division rule (`feed`), `Spool`, seqscan, filter, project, rename, limit, union_all |
| `executor/sort.py` | external merge sort |
| `executor/groupby.py` | group_by and distinct, sort mode and hash mode |
| `executor/joins.py` | nested_loop_join, sort_merge_join, hash_join |
| `executor/setops.py` | union, intersect, except |
| `executor/build.py` | plan validation and construction |
| `executor/run.py` | running a plan, totals, trace |
| `store/` | the storage layer underneath (section 9) |

Conditions are parsed and bound by the Bonus Project 1 code (`source/condition.py`,
`bind` in `source/operators.py`), unchanged.

**Assumptions.** No plan files or dataset were published, so `samples/` holds my own: a small
database (`python -m samples.build`) and one plan per operator, written from the JSON of Section
2. Where Section 2 shows no JSON I assumed: `limit` is `{"op": "limit", "count": n, "child": …}`
(`n` is accepted in place of `count`), and the set operators take `left` and `right` like a join.

## 1. What counts as memory

**A frame of the buffer pool, and nothing else.** The pool has exactly B slots
(`store/buffer.py`). A slot is free, holds a page with a pin count, or is *reserved*. Everything
an operator keeps in memory is one of two things:

| What | How it is held | Who |
|---|---|---|
| a table page being read | `pool.fetch_page`, pinned | seqscan |
| working memory | `pool.reserve(n)` | everything else, through the three classes below |

- `RunWriter` and `RunReader` are one page of a temporary file in memory. Each reserves one frame.
- `Workspace(frames)` is for rows kept in a Python list or dictionary: the block of a nested
  loop, the run being built by a sort, a hash table. It reserves its frames first. Then the
  operator must ask `add(size)` before keeping each row, and the answer is no when the row would
  not fit. Fit is decided by simulating slotted pages being filled one after another (row size
  plus a 4-byte slot, 4088 usable bytes per page), so "three frames of rows" is the same rows
  that fill three pages on disk.

Because the slots are the only frames there are, `pinned + reserved <= B` is not a rule the
operators are trusted to follow. Asking for one more slot when none is free raises
`BufferPoolError`. A plan that broke the budget would stop, not run fast and wrong.
`pool.high_water` records the largest `pinned + reserved` ever seen; `exec trace --verbose`
prints it.

**What is not counted**, stated plainly: the one row in flight between two operators; Python's
per-object overhead (a decoded row is larger than its 12 bytes on a page); the bucket lists of a
hash table, which hold references to rows the workspace has counted; one row of running totals
in a group_by with no keys; and the catalog entries of the tables, which are read once. None of
these grows with the size of the data.

**One page, two forms.** A scan decodes its pinned page into a list of rows. That list is the
content of the frame it holds, not a second frame. A sort writes a run by encoding rows its
workspace already holds, so that writer is created with `own_frame=False` and reserves nothing.

## 2. How B is divided

`assign(frames, limit)` is called once, top down, before the plan runs; the root gets `(B, B)`.

- `frames`: the most an operator *and everything below it* may hold at any moment.
- `limit`: the most they may hold *while the operator streams to its parent*, between the end of
  `open()` and `close()`.
- each operator sets `hold`, what it actually will hold while streaming (`hold <= limit`).

Two numbers, because of time. A blocking child does its heavy work inside its own `open()`,
which its parent calls before reserving anything. The child may therefore use all of `frames`
then. Once it streams, whatever it still holds is taken out of what the parent can work with.
At B = 4 there is no other way to run a sort above a join: they cannot both have three frames at
once, but they do not need them at once.

**The rule, three parts** (`feed()` in `executor/operators.py`):

1. A scan holds one frame, its current page. filter, project, rename, limit and union_all hold
   none of their own.
2. An operator that needs working memory gets every frame its inputs are not holding while they
   stream to it.
3. When an input itself needs working memory while it streams (a join still probing, a sort
   still merging), the two share. The input may hold half, rounded down. If its minimum is more
   than half, it gets exactly its minimum, provided the parent can still do its own work in the
   rest. If even that is impossible, the input is **spooled**: it runs to completion first, with
   the whole allowance and one frame for writing its output to a temporary file, and the parent
   reads that file through one frame.

Spooling is the only time a result is materialised that the algorithm itself did not require,
and it happens only when the frames leave no choice. Its page writes and reads are charged to
the operator that needed it, and `exec trace --verbose` shows it.

One other place spools, by choice and not by necessity: the inner input of a nested loop join
when it is not a plain table scan (section 4.1). The formula of 2.7 has no writes, so this is a
departure, and it is made together with the optimizer: Module 4 defines N as the estimated pages
of the inner *input*, which for a filtered table is far fewer than the table has, and only an
input that has been written down can be read again at that price. The planner charges for the
spool, and its prediction equals the measured I/O (DESIGN_M4.md section 4).

What each operator needs:

| Operator | Fewest frames while streaming | What it does with `frames` = F, `limit` = L, input holding h |
|---|---|---|
| seqscan | 1 | 1 |
| filter, project, rename, limit | as its input | nothing of its own |
| union_all | as its left input | left `(F, L)`; right opens late, see below |
| sort | 1 | runs of F − h pages; merges F − 1 at a time; last merge of at most min(L, F − 1) runs |
| group_by, distinct, sort mode | 1 | the sort's |
| group_by, distinct, hash mode | 3 | table of F − h − 1 frames, 1 for the spill file; later, within min(F, L) |
| group_by with no keys | as its input | nothing: one row of totals |
| nested_loop_join | 3 | block of min(F, L) − h − 1 frames, 1 for the inner |
| sort_merge_join | 2 | two sorts, each ending as one run on disk; 2 readers |
| hash_join | 3 | F − max(h_left, h_right) partitions; table of min(F, L) − 1 frames |
| union | 1 | the sort's |
| intersect, except | 2 | two sorts, last merges of min(F, L) / 2 runs each |

Every operator can stream in at most 3 frames and run in at most 4, so every plan runs at B = 4:
an input that holds 3 is spooled with the 4th.

**Example**, `sort(hash_join(nested_loop_join(R, W), S))` at B = 64, checked by a test:
the sort keeps 32 frames for its runs and lets the hash join hold 32 while it probes (a table of
31). During its partition phase the hash join let the nested loop hold 32 (a block of 30, plus R's
page and W's page) and wrote 64 − 32 = 32 partitions. The peak is 64.

**union_all is the exception to "children open first".** Its right input is opened when the
left one is finished, in the middle of the stream, when the operators above may already hold
everything but L frames. So the right input is given `(L, L)`: it must do all of its work in L,
and union_all declares `hold = L`. If the right input cannot run in L frames at all, it is
spooled inside `open()` instead, while everything is still free. Both halves of this were
bugs before they were rules (DESIGN_LOG.md).

**Determinism.** `assign` depends only on the plan and B. The pool starts empty. The hash
function is the engine's own. So the same plan at the same B gives the same counters, which two
tests check, one of them across processes with different `PYTHONHASHSEED`.

## 3. Sort

External merge sort, `executor/sort.py`.

| Phase | Frames held (F for the sort and its input, input holds h) | Reads | Writes |
|---|---|---|---|
| pass 0 | h + workspace of F − h | the input's own | one page per page of run |
| a merge | up to F − 1 run readers + 1 writer | every page of the runs merged | the same number |
| last pass | one reader per remaining run, at most F − 1 | every page | none |

- **Pass 0** fills the workspace, sorts it with the language's sort (the only place it is
  called, and only on one workspace of rows), and writes the run from those same frames.
- **Runs over a table scan are F pages, not F − 1.** When the workspace is full, the row that
  did not fit and the rest of its page are still in the scan's frame. The sort takes that frame
  over: the scan unpins the page, the sort reserves the frame, the rows change hands
  (`SeqScan.rest_of_page`). The total never exceeds F, and the run is the textbook's B pages.
  Over any other input the runs are F − h pages.
- **Merging** takes F − 1 runs at a time. A run left over without partners is **carried forward
  untouched**, not rewritten.
- **The last merge writes nothing.** It streams through `next()`.
- If the whole input fits in the workspace and the parent lets the sort keep those frames while
  it streams, nothing is written at all: one pass, N reads, 0 writes.
- If the parent needs frames while the sort streams, the last merge is of fewer runs (down to
  one, a single file read through one frame), and more merging happens before it.

**The example of the specification, N = 8, B = 3**, reproduced by
`exec trace --frames 3` on an 8-page table and asserted in a test:

| Pass | What happens | reads | writes |
|---|---|---|---|
| 0 | 3 runs of 3, 3 and 2 pages | 8 (the scan's) | 8 |
| 1 | merge 2 of the 3 runs, carry the third: 2 runs | 6 | 6 |
| 2 | merge the last 2 runs, streaming to the parent | 8 | 0 |
| Total | 3 passes | 22 | 14 |

Comparisons: every call of the key comparison, in the in-memory sorts and in the merge heap.
One call compares two rows on all the sort keys and counts once.

**How values are compared, everywhere** (sort keys, join keys, grouping, set operators,
conditions). Same type only: an INT against a VARCHAR is an error when the plan is built, in
every operator, equality included. VARCHAR order has to be byte-wise on the UTF-8 encoding.
The code never encodes anything to compare it: it compares Python strings, and Python orders
strings by code point. The two orders are the same, because UTF-8 was designed that way: a
larger code point never encodes to a smaller byte sequence, and a string that is a prefix of
another sorts first in both. A test sorts one, two, three and four byte characters, a combining
mark and the empty string, and holds the result against `sorted(key=lambda s: s.encode('utf-8'))`.

## 4. Joins

Output schema: left columns then right columns. Two columns with the same qualified name is an
error when the plan is built.

### 4.1 nested_loop_join

Block nested loop. Fill the block with outer rows; open the inner input and run all of it past
the block, testing every pair; repeat.

- Frames while streaming: outer's h + block + 1 for the inner. Over two scans that is
  1 + (B − 2) + 1. There is no output frame: a matching pair goes straight to the parent. The
  frame the textbook gives to output is the one the outer scan's page sits in.
- Cost over two scans: M + ⌈M / (B − 2)⌉ · N reads, no writes. Measured exactly (REPORT_M2.md).
- Comparisons: one per pair, |R| · |S|, independent of B, of the condition and of the matches.
- **The inner input is read once per block.** A table scan, renamed or not, is opened again.
  Anything else (a filter, a projection, a join) is spooled once and the spool is re-read, so a
  filter below the inner side is evaluated once, not once per block, and N in the formula is the
  size of the spool.
- **Without a condition** every pair is output: a cross product, which the optimizer emits when
  a query has no join predicate.

### 4.2 sort_merge_join

Equality only. Each input is sorted on its key by the sort above, told to end as **one run on
disk**. Then two readers walk the runs in step, one frame each.

Cost: what the two sorts cost, plus one read of each sorted run: sort(M) + sort(N) + M + N.

**Duplicate keys, and a run of equal keys larger than B.** When the keys match, the position of
the first matching right row is marked. For every left row with that key, the right reader
*seeks back to the mark* and reads the run of equal keys again. Each left row therefore meets
each right row: R.b = [1, 2, 2, 3] and S.b = [2, 2, 4] give four rows (a test).

The run of equal right rows is never held in memory, whatever its length. It is already on disk,
in the sorted file, and that file *is* the spill. Rewinding costs page reads instead of frames:
one read each time the reader goes back to an earlier page. With one key in 40 percent of the
rows this is expensive (11,735 reads in the report against 460 for the hash join), but it is
correct at any B and it never assumes a run fits.

Comparisons: those of the two sorts, plus every key comparison of the merge.

### 4.3 hash_join

Grace hash join, equality only, left is the build side.

| Phase | Frames held | Reads | Writes |
|---|---|---|---|
| partition left | h_left + P writers | the input's own | M, plus up to P part-filled pages |
| partition right | h_right + P writers | the input's own | N, plus up to P |
| probe one pair | table of H − 1, and 1 reader | the pages of the pair | none |

P = F − max(h_left, h_right), which is B − 1 over two scans. H = min(F, L), B at the root.
Partitioning uses hash function 0 of `hash_key`; the in-memory table uses a different one.

**Partition overflow: both remedies, in order.** A left partition that does not fit in H − 1
frames is never loaded. The pair is partitioned again with the next hash function into H − 1
parts (one frame reads, H − 1 write). That is done up to `MAX_LEVEL = 3` levels of partitioning
in all, the first included. A pair still too large after the third is joined by **block nested
loop**: blocks of H − 2 frames of left rows, the right file read once per block.

Why both. Splitting again fixes a partition that is too large because many different keys landed
in it, which is what happens when B is small against M (P · (H − 1) pages is all one level can
place). It cannot fix a partition that is too large because one key is in most of its rows: every
hash function sends equal keys to the same place. For that only a join that needs no table
works. Three levels bounds the rewriting done before giving up.

Comparisons: one for every left row whose key is compared with a right row's key, in a bucket of
the table or in the nested loop. Partitioning compares nothing.

## 5. group_by and distinct

distinct is group_by on every column with no aggregates; it is the same class.

- **Sort mode.** The external sort on the grouping columns, then one walk: a group ends where
  the key changes. Costs what the sort costs.
- **Hash mode.** A table of groups in the workspace. While a new group fits, it is added. When
  the table is full it is frozen: rows of groups it has still update them, other rows go to a
  spill file. At the end of the input the groups in the table are complete and are output. The
  spill file is split into H − 1 partitions by a hash of the key and each is grouped the same
  way with a new table; a partition that overflows spills and is split with the next hash
  function. After three rounds a spill file is grouped by sorting it. If the table never
  fills, hash mode does no I/O of its own.
- **No grouping columns.** One group, no sort, no table, one output row. On an empty input that
  row still exists: COUNT is 0, and any other aggregate raises
  `sum(E.b) of an empty input is undefined`. With grouping columns an empty input gives no rows.

SUM and AVG need INT (checked when the plan is built). AVG is a real number, a third type REAL
that exists only as the result of AVG, written to four decimal places.

**A SUM can be larger than an INT.** Three values of two billion add up to six billion, which
4 bytes cannot hold. In memory that is no problem, Python's integers have no limit, and the
value prints. In a temporary file it was one: a join input, a sort run, a partition all encode
rows with the record format, and the first version encoded a SUM as a 4-byte INT and died with
a stack trace. A SUM column is now written to temporary files as 8 bytes (`(INT, 8)` in
`store/record.py`). It is still an INT wherever types are checked or printed, a table's INT is
still 4 bytes on disk, and a number too large even for 8 bytes is a one-line error. Output names are
`count_star`, `avg_R_salary`, `max_R_name`.

Comparisons: sort mode, the sort's plus one per row to see whether the group continues; hash
mode, one per row looked up in the table.

## 6. Set operators and limit

Same number of columns, same types position by position; names from the left.

- **union_all**: left rows, then right rows. No memory of its own (but see section 2).
- **union**: union_all followed by duplicate removal by sorting, the same code as distinct.
- **intersect, except**: both inputs are sorted on all columns by the external sort and walked
  in step. A left row is output once if it is (intersect) or is not (except) on the right, and
  its copies are skipped. Neither side is ever read into memory whole.
- **limit**: after `count` rows it returns end-of-stream without calling its child. A test
  checks that `limit 5` over an 8-page table reads one page.

## 7. Counters

Each operator has its own four counters. `tuples_out` is rows returned from `next()`.
`page_reads` and `page_writes` are disk manager calls made on its behalf: table pages it fetched
that were not already in the pool, and every page of its temporary files. A sort used inside
another operator (sort_merge_join, sort-mode group_by, union, intersect, except) reports on that
operator, because the plan has no node for it. A spool is charged to the operator that needed
it.

The sum of all operators' page counters equals what the disk manager did while the plan ran (a
test). Reading the catalog happens when the plan is built and belongs to no operator.

**What `exec trace` prints.** `tuples_out` and `comparisons` on a line are that operator's own.
`reads` and `writes` on a line are those of the operator *and of everything below it*. That is
what the example in section 4 of the specification shows: a hash join over two 24-page scans
with `reads=96 writes=48`, while each scan below it shows `reads=24`. 96 is the scans' 48 plus
the 48 the join read back from its partitions, and 96 + 48 is 3 (M + N), the formula of section
2.9, which counts reading the inputs as part of the join. Read this way, a join's line can be
held against its formula directly: a block nested loop over two scans shows
M + ⌈M / (B − 2)⌉ · N on the join's line, where its own count is 0 because every one of those
reads is made by a scan. The top line is the whole plan and equals the totals of `exec run`.

`--own` prints each operator's own page counts instead (the join above: 48), which are what
`counters()` returns and what add up to the totals. `trace --json` gives both for every node,
as `page_reads` / `page_writes` and `subtree_page_reads` / `subtree_page_writes`. The first
version printed own counts by default and the specification's numbers only on request; that was
the wrong way round for a tool whose reader holds its output against the specification.

## 8. Plans and errors

`validate()` checks shape before anything is looked up, and names the node and its place:
`Node 'sort' at $.child: missing required field 'keys'`. `build()` then resolves tables, columns
and conditions bottom up, so an unknown table, an unknown column, a type error in a condition
and a duplicate column name are all reported before any data page is read. Malformed JSON is
reported with line and column. During a run, `rows()` always closes the root, and then checks
that no page is pinned, no frame reserved and no temporary file left.

**Errors that only show when the plan runs.** Two exist: an aggregate other than COUNT over an
empty input with no grouping columns, which the specification makes an error, and a row too
wide for one page of a temporary file. Both are raised in `open()` or on the first `next()`.
`exec run` writes its rows as they arrive, so it must not have printed anything by then:
`Execution.started()` opens the plan and fetches the first row *before* the schema line or the
opening of the JSON object is written. The output of a failing plan is therefore one line
starting with `error:`, as for a plan rejected before it ran.

**One operator can fail later than that.** `union_all` opens its right input only when the left
one is finished, so a right input that fails does so after the left rows have been written, and
fetching the first row in advance cannot help. Nothing can take those rows back, and holding
all output until the end would put the whole result in memory. So `exec run --json` closes the
object properly instead: the rows that came out, then an `"error"` member with the message, then
the totals. It is valid JSON, the exit code is 1, and the error line goes to standard error.

**Not in the specification, accepted here**, so that plans from Modules 3 and 4 can be run:

| Addition | Why |
|---|---|
| `{"op": "rename", "to": "e", "child": ...}` | a self join needs two names for one table |
| a project column `["R.a", "x"]` | `SELECT a AS x` |
| a third element in an aggregate, `["count", "*", "n"]` | `COUNT(*) AS n` |
| `n` in place of `count` on limit | the field name is not given in the specification |
| `nested_loop_join` with no `condition` | a cross product |

## 9. What this stands on: the storage layer

Module 2 calls only the Module 1 contract: `fetch_page`, `unpin_page`, the catalog, the disk
manager's counted reads and writes, plus `reserve` / `release` for working memory. The storage
layer is described in DESIGN_M1.md. Three facts about it matter here.

- **Temporary files use the same slotted page** as tables (8-byte header, 4-byte slots), so a
  sorted run of a table has as many pages as the table, and a workspace of k frames holds what
  k pages hold.
- **seqscan reads M pages and holds one frame, and does not read the directory.** A scan that
  follows a table's directory pages (`HeapFile.scan`, which `storage scan` uses) keeps a
  directory page pinned beside the data page: two frames, and M + ⌈M / 681⌉ reads. This module
  requires one frame and M reads. A table file is always directory, 681 data pages, directory,
  681 data pages, …, because pages are only added at the end, so the data pages in directory
  order are the page ids that are not multiples of 682. The executor's scan walks those. A test
  on a table with two directory pages checks that the rule and the directory agree.
- **The engine was first built on a storage layer of my own design**, before the Module 1
  specification was available, and that layer was then replaced. Only `SeqScan` and two lines of
  `RunWriter` changed in `executor/`. All the measurements of REPORT_M2.md were run again on the
  final layer and every counter came out the same, because rows per page did not change for
  these tables (255 for three INTs, 19 for the wide rows).

## 10. Tests

`tests/test_exec.py` has the checklist of the specification as its first class, then one class
per group of operators, the budget and the counters, plan errors, and the command line.
`tests/test_exec_fuzz.py` runs 160 random plan trees at B = 4, 5, 8 and 23 and compares each
with the Bonus Project 1 engine running the same plan in memory, also checking the peak and the
clean-up every time. `tests/test_store.py` covers the storage layer, including that evicting a
pinned page fails.
