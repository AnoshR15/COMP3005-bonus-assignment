# DESIGN_M4.md — Query optimizer

A logical plan goes in, a physical plan comes out, chosen from numbers.

```
logical plan ──> rewrite rules ──> estimate rows ──> cost ──> enumerate join orders ──> physical plan
   (Module 3)      rewrite.py        estimate.py     cost.py        planner.py            (Module 2)
                                          ▲
                                   statistics in the catalog, written by ANALYZE (stats.py)
```

| File | Contents |
|---|---|
| `opt.py` | command line: analyze, plan, explain, dp-table |
| `optimizer/stats.py` | ANALYZE and the histogram |
| `optimizer/estimate.py` | selectivity and cardinality |
| `optimizer/rewrite.py` | the seven rewrite rules |
| `optimizer/cost.py` | the cost formulas |
| `optimizer/planner.py` | physical choices, join enumeration, explain, the DP table |

**Assumption.** No dataset or queries were published, so the measurements of REPORT_M4.md use an
order database and twenty queries of my own, with three pairs of correlated columns built in;
its section 0 gives the schemas, how the rows are made, and every query.

## 1. Statistics

### What is stored, and where

In the table's catalog entry, as its `stats` member (DESIGN_M1.md section 7; an entry spans as
many catalog pages as it needs):

```json
"stats": {"n_tuples": 2000, "n_pages": 40, "width": 76.1, "buckets": 20,
          "columns": {"odate": {"ndv": 878, "min": 1, "max": 1000, "width": 4.0,
                                "hist": [[1, 54, 100, 45], [55, 101, 103, 42], ...]}, ...}}
```

`n_pages` is data pages, without directory pages. `width` is the average record length. A
histogram bucket is `[low, high, tuples, distinct values]`. A column's own `width`, its average
encoded length, is an addition to the specification's list: it is what lets the output of a
projection be sized. Row and page counts are also in the entry itself, kept current by every
insert, and the optimizer uses those, so a table loaded after its last ANALYZE is still sized
correctly.

### How ANALYZE computes them

Exactly, with no sampling, and without ever holding a column in memory:

1. **One scan of the table.** It counts and measures the rows and writes every column to its
   own temporary file. Frames: one for the scan, one per column file.
2. **Each column file is sorted with the external sort of Module 2**, in the frames of the
   buffer pool, like any sort in a query.
3. **One walk over the sorted values** yields the number of distinct values (count the changes),
   the lowest and highest (first and last), and the histogram.

A table with more columns than frames − 1 is scanned once per batch of columns. A test analyzes
a 12-page column with 4 frames and checks the pool never held more than 4 and that the result
equals the one computed with plenty of memory.

Running ANALYZE again on an unchanged table writes the same bytes to `catalog.db` (a test
compares the file).

### The histogram

Equi-depth: sort, cut every n / k values, k = `--buckets`, default 20. Two refinements, both
about equal values:

- **A cut is never made inside a run of equal values.** Otherwise a value could be the `high`
  of one bucket and the `low` of the next, and "the bucket holding v" would be two buckets.
- **A value whose run alone is at least n / k tuples gets a bucket to itself.** Its estimate is
  then its exact count. Without this a value held by 70 percent of the rows shares a bucket
  with its neighbours and is estimated as their average.

So buckets hold about n / k tuples, more when a common value overshoots, and there may be a few
more or fewer than k. A column of at most k distinct values ends up with one bucket per value,
and equality on it is exact.

## 2. Selectivity

| Predicate | Selectivity |
|---|---|
| c = v | the bucket holding v: tuples(bucket) / (n × ndv(bucket)); no histogram: 1 / ndv(c); v outside [min, max]: 0 |
| c < v, c > v | whole buckets on the right side, plus the fraction (v − low) / (high − low) of the bucket holding v, or its mirror; no histogram: that fraction of [min, max] |
| c <= v, c >= v | sel(c < v) + sel(c = v), and the mirror |
| c <> v | 1 − sel(c = v) |
| p AND q | sel(p) × sel(q) |
| p OR q | sel(p) + sel(q) − sel(p) × sel(q) |
| NOT p | 1 − sel(p) |
| R.a = S.b | rows = n(R) × n(S) / max(ndv(R.a), ndv(S.b)) |
| any other comparison of two columns | 0.1 |

A node's output is its input times the selectivity, rounded up, never below 1. The worked example
of the specification (did = 7 → 200, age > 60 → 2,000, both → 40, the join → 10,000) is a test.

That example is the formulas *without* a histogram: 10 / 50 is (max − v) / (max − min) over the
whole column. Loaded as real data, 10,000 rows with ages 20 to 70, and analyzed with
`--buckets 1`, the tool prints exactly those four numbers. With the default 20 buckets it prints
200, 2,058, 42 and 10,000, and the true count for age > 60 is 1,960. The histogram made the
estimate *worse*, and the reason is the rule itself: each age is 196 rows, so a bucket of 500
closes after three ages, 59 to 61; the linear fraction of that bucket above 60 is
(61 − 60) / (61 − 59) = one half, where one age of three is above 60. An integer column is
treated as a continuous one, and the narrower the bucket the more that costs. The fraction for
integers should be (high − v) / (high − low + 1), which gives 1,960 here; I kept the
specification's formula because its worked example is computed with it.

**Assumptions, which is where these break:** values are spread evenly inside a bucket;
integers are treated as continuous (so `k < 400` on 0..569 is estimated 401, not 400); different
columns are independent; in a join, every value of the side with fewer distinct values finds a
partner on the other side. REPORT_M4.md section 4 breaks the third one.

### What I changed, and why

1. **A filtered column keeps a cut-down histogram.** After `a > 10`, column a is no longer
   distributed as in the table. `restrict()` cuts its histogram to the part that passed, and a
   later predicate on a is measured against that. So `a > 10 AND a < 20` is estimated as the rows
   between 10 and 20. Multiplying would give sel(a > 10) × sel(a < 20), which for 0..99 is
   0.89 × 0.2 = 0.18 where the truth is 0.09; and `a > 50 AND a < 30` would be 0.15 where the truth
   is nothing. This only helps predicates on the *same* column. Different columns are still
   multiplied.
2. **Pages are ceil(rows × (width + 4) / 4088)**, not ceil(rows × width / 4096). Each row costs
   a 4-byte slot and a page has 4,088 usable bytes (Module 1). For 12-byte rows the difference is
   a third, and with it an estimated table size agrees with the real one.
3. **Distinct values are capped by rows.** A column with 300 distinct values in a stream
   estimated at 40 rows is taken to have 40.
4. **Strings.** There is no linear fraction of a range of strings, so a range predicate keeps
   half of the bucket holding v.

### With no statistics

A table never analyzed still has its row and page counts, from the catalog. Otherwise:
equality keeps 0.1, a range keeps 1/3, a join predicate keeps 0.1 (the first two are Selinger's
defaults), an INT is 4 bytes wide and a VARCHAR(n) is 2 + n / 2, and a group_by on a column of
unknown distinct count is taken to produce a tenth of its input. A test plans and runs a
three-way join over tables that were never analyzed.

## 3. Rewrite rules

Applied to the logical plan before anything is costed. Each precondition is checked on column
names resolved against the schema of the node in question.

| # | Rule | Precondition, as implemented |
|---|---|---|
| 1 | select[p AND q] becomes two selects | none |
| 2 | push a select below a join or cross | every column it references resolves on one side, and none on the other |
| 3 | cross + select becomes join; above a join, the predicate is added to its condition | the predicate is `x = y` with x on one side and y on the other |
| 4 | push a select below a rename, union or union_all | every column maps to a column below; names are rewritten (for a union, separately for each branch) |
| 5 | push a select below a group_by | every column it references is a grouping column |
| 6 | push projections down | a column is kept if anything above needs it or the operator itself does |
| 7 | drop a projection that drops nothing; merge adjacent selects | the projection's list equals its input's columns, in order |

Rules 1 to 5 are one pass: each select is split and every piece is sunk as far as it will go.
Rule 6 follows, because a projection stops a select from sinking. Then rule 7.

Decisions inside the rules:

- **Rule 4 leaves a rename directly over a scan alone.** There is nothing below it to reach, and
  the specification's own example keeps `select e.age > 60` above `rename e (scan Emp)`.
- **Rule 4 also passes a projection.** A projection is a rename that drops columns; a column that
  was given a new name is rewritten to its source. Without this a predicate on a subquery in FROM
  stops at the subquery's select list and rules 4 and 5 never get to act on what is inside.
- **Rule 6 lists what is needed above first.** The columns of an inserted projection are in the
  order of the rule's own sentence: the columns the ancestors need, in the order they need them,
  then the ones the operator needs itself. That is why the example reads
  `project [e.name, e.did]`: `e.name` for the select list, `e.did` for the join. The first
  version listed them in the table's column order, which gave the same line only because `name`
  comes before `did` in my Emp; on a table declared the other way round the example did not
  reproduce. A test now uses Proj, where the join column comes first.
- **Rule 6 puts a projection** on each input of a join block and below a group_by or sort, when
  that input carries columns nobody needs. An existing projection is trimmed. A projection is
  never left empty: `COUNT(*)` needs rows, so one column stays. What a *join result* should
  carry is not decided here, because it depends on the join order; the planner projects each
  intermediate join result to what is still needed once the order is known.
- **Nothing is ever pushed past an aggregate.** A predicate on an aggregate's output stays above
  the group_by: that is HAVING.

The example of Section 3 of the specification reproduces line for line (a test, and
`opt explain --rewrite`):

```
before                                                    after
  project [e.name]                                          project [e.name]
    select e.did = d.did and e.age > 60 and d.budget > 1000   join e.did = d.did
      cross                                                     project [e.name, e.did]
        rename e (scan Emp)                                       select e.age > 60
        rename d (scan Dept)                                        rename e (scan Emp)
                                                                project [d.did]
                                                                  select d.budget > 1000
                                                                    rename d (scan Dept)
```

Two tests guard the preconditions from the other side: for every test query, the rewritten plan
returns the same rows as the original on the Bonus 1 engine, with all rules on, with each rule
off, and with all off; and rewriting a rewritten plan changes nothing.

## 4. Cost model

Page I/O, reads plus writes. M and N are estimated pages of the left and right input.

| Operator | Cost |
|---|---|
| seqscan | M |
| filter, project, rename, limit | 0 |
| sort | 2 M (passes − 1) + M, passes = 1 + ⌈log(⌈M / B⌉) / log(B − 1)⌉ |
| nested_loop_join | M + ⌈M / (B − 2)⌉ N |
| sort_merge_join | sort(M) + sort(N) + M + N, **plus M + N** |
| hash_join | 3 (M + N), **plus 2 (M + N) for each extra level of partitioning** |
| distinct, group_by | sort mode: the sort. hash mode: M if the groups fit in B − 2 frames, else M + 4 M × (share of the groups that do not fit) |

### Where this differs from the specification, and which module is right

Both differences make the optimizer agree with what the executor does, which REPORT_M2.md
measured. In both the executor is right and the formula was changed.

- **sort_merge_join.** The executor writes each sorted input as one run and then reads it for
  the merge; it has to, because rewinding over duplicate keys needs the run on disk. The
  specification's `sort()` stops before that last write. Measured at M = N = 240, B = 16: 2,400.
  The formula with the extra M + N gives 2,400. Without it, 1,920.
- **hash_join.** 3 (M + N) holds while a partition of the build input fits in memory, M ≤ (B − 1)².
  Beyond that every pair is partitioned again. Measured at M = N = 240, B = 16: 2,992. The
  formula with one extra level gives 2,400; 3 (M + N) alone gives 1,440 and would have made the
  optimizer choose a hash join exactly where it is worst.

The nested loop and the sort are the specification's formulas unchanged; both matched the
executor exactly in REPORT_M2.md.

### How the formulas add up

Each formula includes reading its inputs once: the M in the sort, the M and N in the joins. So:

- **An input that is a table access** (a scan under filters, projections and renames): the
  formula paid for P pages, the size of its output, but reading the table cost its full size.
  The difference is added. For a plain scan there is none.
- **An input that is anything else** (a join, a sort, a subquery): its whole cost is added, and
  the formula charges P again for reading it. That is the specification's convention; its
  example in 5.1 adds 110 + 20 + 400. My executor does not do that read when the input is
  pipelined, so this is a known over-charge of one P per such input.
- **An inner input of a nested loop that is not a plain (possibly renamed) scan** is spooled once
  by the executor: its cost, plus N to write the spool, plus N per block. N here is what the
  specification says it is, the estimated pages of the *input*, which for a filtered or
  projected table is much less than the table. Only an inner input that has been written down
  can be read again at that price; opening the table again for every block would cost the
  table's full size each time. So the executor's spool and this formula are one decision, and
  they agree: for R (80 pages) joined with S (20) on a query that keeps the wide column, the
  plan chosen at B = 16 is predicted at 240 and measured at 240, at B = 32, 180 and 180.

### What `est_cost` on a line of explain is

Three numbers can be printed for a node, and they answer different questions.

| `opt explain` | est_cost on a line | Use |
|---|---|---|
| default | the operator's formula from the table above, applied to the estimated pages of its inputs: M for a scan, 0 for a filter or a projection, 3 (M + N) for a hash join | checking a line by hand against the formula. This is the form of the specification's example: 0 on the filter, the pages on each scan, the formula on the join |
| `--subtree` | the estimated cost of everything from that node down | the number the join enumeration compares; the top line is the cost of the plan. Put beside `exec trace`, whose page counts are also for everything below a line |
| `--own` | the node's share: its subtree minus its inputs' subtrees | lines that add up to the cost of the plan. Put beside `exec trace --own` |

The default lines do not add up to the plan's cost, because a join's formula and a scan's both
count reading the table. The first version printed the `--own` number by default. A nested loop
join over two scans that fits in one block then showed `est_cost=0`, since all of its formula,
M + N, was already on the two scan lines: correct arithmetic, and a line that looks as if the
join were free to anyone holding it against Section 4.

For a nested loop whose inner input is spooled the formula on the line is M + N + ⌈M / (B − 2)⌉ N:
the spool written once, then read once per block.

In the prompt, `EXPLAIN` is `opt explain`, and `EXPLAIN ANALYZE` prints `est_cost` and
`actual_io` both in the `--subtree` sense, like against like.

### What the model does not know

- **The executor divides B among operators that work at the same time** (DESIGN_M2.md section
  2). The model gives every operator all of B. For one join over two tables they agree. For a
  nested loop join feeding another join they do not: at B = 7 the lower one gets a block of one
  frame, not five. REPORT_M4.md section 4 shows a join predicted at 64 that cost 510.
- The part-filled last page of every hash partition.
- That a limit stops its input early, and that a merge join stops when one side ends.

## 5. Choosing

### Operators other than joins

scan → seqscan, select → filter, and project, rename, sort, limit and the set operators map one
to one. distinct and group_by get the cheaper of sort mode and hash mode by the formulas above,
hash on a tie. A group_by with no keys needs neither.

### Join enumeration

A **join block** is a maximal tree of join and cross nodes. Its leaves are whatever hangs below,
each planned on its own. Its predicates are the conjuncts of the join conditions (rule 3 is what
turns a WHERE equality into one).

Dynamic programming over subsets of the leaves, left-deep:

```
best[{r}] = the plan of leaf r
for k = 2 .. n:
    for every subset S of size k:
        best[S] = cheapest, over r in S and each algorithm, of   best[S − {r}]  join  r
```

- **The right input is always a leaf.** `best[S − {r}]` is the left, outer input.
- **Size depends on the subset, not the order.** rows(S) is the product of the leaves' rows and
  the selectivities of every predicate inside S, so every way of building S is costed against the
  same output.
- **A pairing with no predicate between the two sides is skipped.** Only if the block cannot be
  joined at all without one (the query really is a cross product) is a second pass run that
  allows them, and the DP table marks them.
- **Algorithms per pairing.** Nested loop with every predicate that has just become applicable.
  If one of them is an equality between the two sides, also hash join and sort-merge join on the
  equalities, with the rest as a filter above the join.
- **Ties** go to the pairing tried first: leaves in written order, nested loop before hash before
  sort-merge. That makes the result a function of the query alone.
- **Conditions are written one way.** `a = b` and `b = a` are the same predicate; an equality is
  emitted outer column first, and predicates are ordered by text. That, with rule 3, is why a
  query written with commas and the same query written with JOIN ON give the identical physical
  plan (a test, on two-, three- and four-table queries).
- **Column order.** A reordered join produces its columns in another order. Where the consumer
  depends on position (the final result, a set operator) a projection puts them back.
- Up to 10 leaves are enumerated (1,023 subsets). Beyond that joins stay in written order.

### Why left-deep, and what it gives up

**What it buys.** The search space. With n relations there are n! left-deep orders, and the
table needs one entry per subset: for n = 6, 63 entries and 186 pairings to cost. Bushy trees
add every way to split a subset in two, on the order of 3ⁿ pairings, and the number of tree
shapes grows with the Catalan numbers on top of the orders. And the right input of every join is
a base relation: the outer side is never materialised, it streams from one join into the next;
the inner side is something a nested loop can read again and a hash join can read once, without
first having to produce it.

**What it gives up.** Plans in which two intermediate results are joined to each other. For
`(A ⋈ B) ⋈ (C ⋈ D)` with a selective predicate inside each pair and a weak one between B and C, the
bushy plan joins two small results. A left-deep plan must carry one pair through the weak join
before the other pair's predicate can shrink it. Chains and stars are served well by left-deep
trees; queries with two selective clusters are not.

**What it costs in my executor specifically.** The pipelined outer side is not free: joins that
run at the same time share B (section 4). A deep left-deep pipeline of nested loop joins is
where that hurts most.

### The example of Section 5.1

Reproduced by a test and from the command line with `--assume`, which supplies the example's
sizes in place of computed ones:

```
subset    best plan considered                                                      rows  pages  cost
{A}       scan A                                                                    5000    100   100
{Bp}      scan Bp                                                                    500     10    10
{C}       scan C                                                                   20000    400   400
{A,Bp}    {Bp} outer, nested loop with A inner: 10 + 1 x 100                        1000     20   110
{A,C}     no join predicate, skipped
{Bp,C}    {Bp} outer, nested loop with C inner: 10 + 1 x 400                        2000     40   410
{A,Bp,C}  {A,Bp} outer, nested loop with C inner: 110 + 20 + 1 x 400                4000     28   530
          alternative: {Bp,C} outer, nested loop with A inner: 410 + 40 + 2 x 100                 650
```

Every row agrees with the specification except the alternative, which it gives as
410 + 40 + 100 = 550. With 40 pages of outer and B − 2 = 30 frames of block, the formula reads the
inner twice: 410 + 40 + 2 × 100 = 650. The specification's own sentence under the table says the
winning plan is the one whose intermediate result fits in B − 2 frames, which is the same
observation. The winner, 530, is not affected.

## 6. Interface

```
opt analyze  --catalog DIR --table R [--buckets 20]
opt plan     --catalog DIR --logical q.json --frames 32 --json
opt explain  --catalog DIR --logical q.json --frames 32
opt dp-table --catalog DIR --logical q.json [--frames 32]
```

`--catalog` and `--db` are one option, the database directory, and without it the directory is
`db`. `--frames` is 32 unless given, so `opt dp-table --logical q.json --catalog db/` runs as the
handout writes it. The table depends on B, so the default is stated here and not left implicit.

`opt plan --json` prints a plan with only the fields Module 2 accepts; the estimates live in the
optimizer's own tree and are shown by explain. The output of `opt plan` is fed to `exec run`
unedited in a test.

Additions, none of which change the required forms:

| Option | What for |
|---|---|
| `--sql "SELECT ..."` | in place of `--logical`: compile with Module 3 first |
| `explain --rewrite` | the logical plan before and after the rules |
| `explain --subtree`, `explain --own` | the other two meanings of est_cost (section 4) |
| `--no-rule N` | leave out rule N: how REPORT_M4.md measures each rule |
| `plan --naive` | the unoptimized plan: operators as written, joins in written order, block nested loop for every join, sort mode for grouping |
| `--assume FILE` | row and page estimates to use in place of the computed ones, for a leaf or a set of leaves. "What would be chosen if the estimate were right?" |
| `dp-table --alternatives` | every pairing that was costed, not only the best per subset |
| `analyze --frames`, `--all` | memory for the sorts; every table |

## 7. What Module 2 gained for this module

- `nested_loop_join` without a `condition` is a cross product. The optimizer needs something to
  emit when a query has no join predicate.
- A nested loop join re-opens an inner input that is a table scan *under renames*, not only a
  bare scan. Every aliased table is a rename, and spooling each of them wrote a copy of the
  table for nothing.

## 8. Tests

`tests/test_opt.py`. The first class is the checklist of Section 8 of the specification, one
test per line. Then statistics, selectivity, each rewrite rule with its precondition, the cost
formulas, enumeration, explain and the command line. Every plan the optimizer emits for nineteen
queries is run in the executor at B = 4, 8 and 32 and compared with the Bonus 1 engine's answer
to the logical plan.
