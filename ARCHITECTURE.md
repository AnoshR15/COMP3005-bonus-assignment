# ARCHITECTURE.md

A database system in four modules and one prompt. SQL text goes in; rows come out, read from
4 KB pages on disk through a buffer pool of B frames.

```
  SQL text
     │
     ▼
  [ M3 ]  sqlfront/     lexer → parser → name resolution, types, aggregates → translation
     │
     │  logical plan (JSON)
     ▼
  [ M4 ]  optimizer/    rewrite rules → estimates → cost → join order → algorithms
     │                                    ▲
     │  physical plan (JSON)              │ statistics, written by ANALYZE
     ▼                                    │
  [ M2 ]  executor/     open / next / close, every operator inside B frames
     │                                    │
     │  fetch_page, unpin_page, reserve   │
     ▼                                    │
  [ M1 ]  store/        buffer pool → heap files → pages in <table>.db and catalog.db
```

Underneath all four sits the Bonus Project 1 engine, `source/`: its condition parser and binder
are used by M2, M3 and M4, its plan reader by the tests, and the engine itself is the reference
every other module is tested against.

## 1. The pieces

| Module | Package | Tool | What goes in and out |
|---|---|---|---|
| M1 storage | `store/` | `storage.py` | page ids in, pages out; tables, rows, the catalog |
| M2 execution | `executor/` | `exec.py` | physical plan in, rows and counters out |
| M3 SQL front end | `sqlfront/` | `sql.py` | SQL in, logical plan out |
| M4 optimizer | `optimizer/` | `opt.py` | logical plan in, physical plan out; ANALYZE |
| integration | | `minidb.py` | SQL in, rows out |
| Bonus 1 | `source/` | `ra.py` | relational algebra, conditions, the reference engine |

`minidb.py` is wiring. It contains no parsing of SQL, no planning and no page access of its own:
for a query it calls `compile_sql`, `Planner.optimize`, `Execution`, in that order, and prints.
The four module tools work on the same database directory unchanged (a test runs `storage scan`
and `opt explain` on a database built through the prompt).

## 2. The interfaces

Each is one of two things: a JSON document, or a small set of calls. A module depends on the
interface below it and nothing else below it.

### M3 → M4: the logical plan

A JSON tree. Operators and fields (DESIGN_M3.md section 5, `source/plan_json.py`):

```
scan {table}                        select {condition, child}        project {columns, child}
rename {to, child}                  join {condition, left, right}    cross {left, right}
group_by {keys, aggregates, child}  distinct {child}                 sort {keys, child}
limit {count, child}                union, union_all, intersect, except {left, right}
```

Every column is `alias.column`, except names the query created (an aggregate's output, a
select-list alias). Conditions are strings. Nothing in it names an algorithm, and nothing has
been moved from where the SQL put it.

### M4 → M2: the physical plan

The same shape with physical operators (`executor/build.py`):

```
seqscan {table}                     filter {condition, child}        project, rename, limit, sort
distinct {mode, child}              group_by {mode, keys, aggregates, child}
nested_loop_join [condition]        sort_merge_join, hash_join {condition, left, right}
union_all, union, intersect, except
```

`mode` is `sort` or `hash`. `opt plan` prints exactly this and `exec run` takes it unedited; the
optimizer's estimates never enter the plan, they stay in its own tree and are shown by explain.

### M2 → M1: pages

```
pool.fetch_page(file, page_id)   pool.unpin_page(file, page_id, dirty)
pool.reserve(n), pool.release(n)                      working memory, in frames
catalog.get_table(name) → columns, file, pages, tuples
disk.create_temp()                                    temporary files, counted like any I/O
store.page.records(page)         RecordCodec          the page and record formats
```

The executor never opens a file. All its memory is pool frames, pinned or reserved, which is
what makes B a property of the pool and not a promise.

### M4 → M1 and M2: statistics

ANALYZE runs a scan and one external sort per column using M2's operators, and writes a `stats`
object into the table's catalog entry through M1. The planner reads it back from the catalog.
That is the only state the optimizer keeps.

### M3 → M1: schemas

`sqlfront.catalog.load_catalog(directory)` reads table and column definitions from `catalog.db`.

### Everyone → Bonus 1: conditions

`parse_condition(text)`, `bind(tree, schema)`, `condition_text(tree)`. M3 writes every condition
with `condition_text`; M4 parses, rewrites and re-writes them; M2 parses and binds them. The
strings that travel between modules are produced and consumed by one piece of code.

## 3. One query, start to finish

```
SELECT d.title, COUNT(*) AS n FROM Emp e JOIN Dept d ON e.did = d.did WHERE e.age >= 30 GROUP BY d.title
```

1. **M3** resolves `e.age`, `d.title` against the catalog and emits
   project ← group_by ← select ← join ← (rename e ← scan Emp, rename d ← scan Dept).
2. **M4** pushes `e.age >= 30` below the join, puts projections on both inputs, estimates rows
   from the histograms, costs the two join orders under three algorithms, picks one, and picks
   hash or sort for the group_by.
3. **M2** builds the operators, divides B among them, opens the root, and pulls rows.
4. **M1** serves each `fetch_page` from the pool or the disk, and counts.

`EXPLAIN ANALYZE` in the prompt shows steps 2 and 3 side by side, line by line: estimated rows
against actual rows, and estimated cost against actual I/O, each for everything from that line
down, so the top line is the whole plan.

## 4. The workload

No dataset or queries were published for the integration checkpoint, so `workload/` holds my
own: the order database as six CSV files, and twenty five queries (REPORT_INTEGRATION.md).

```
python -m workload.build                                         the CSV files
python minidb.py --db wdb --file workload/load.sql               CREATE TABLE ...; LOAD t FROM 'file'; ...; ANALYZE;
python minidb.py --db wdb --file workload/queries.sql --report   one line per query
python -m workload.answers --db wdb                              the answers as files, to compare with PostgreSQL's
```

`--report` prints, per query: result rows, page reads, page writes, wall time, the rows estimated
at the root, the actual rows, their q-error, and the optimizer's estimated cost. Each query runs
in a fresh buffer pool, so its page counts do not depend on the queries before it and are the
same on every run. Any other dataset goes through the same three commands: a file of
CREATE TABLE and LOAD statements, then a file of queries.

## 5. What I would do differently

**Read each module's specification before writing anything it constrains.** The storage engine
was built twice. The first version came from the overview alone and differed from the Module 1
document in nine places, among them the page header and the file layout. The plan format was
guessed before Module 3's document fixed it, and three names were wrong. What limited the damage
both times was the interface: the executor uses five calls of the storage layer, so replacing
everything behind them changed one operator. I would keep that discipline and drop the guessing.

**Give the cost model and the executor one shared description of memory.** The executor divides
B among operators that run at the same time; the optimizer assumes each has all of it. For a
single join they agree. For two nested loop joins in a pipeline at B = 7 the lower one gets one
frame of block where the model assumed five, and a join predicted at 64 pages cost 510. The rule
that divides the frames is one function in the executor. It should be a function both modules
call. This is the largest known error in the system and it is not a statistics problem.

**Divide the frames by need, not in halves.** Giving a streaming input half of what its parent
has is simple to state and often wrong: a join that needs two frames gets sixteen while the
sort above it starves. With row estimates available from the optimizer, the split could follow
the estimated sizes.

**One home for the plan formats.** The logical format is validated in `source/plan_json.py`, the
physical one in `executor/build.py`, and the optimizer builds physical plans as dictionaries by
hand. Three places know the field names. A single table of operators and fields, used to
validate, to build and to emit, would have caught the wrong names on the day they were written.

**Statistics about pairs of columns.** Every estimate that was badly wrong for a reason in the
data was wrong for the same reason: two columns that move together, multiplied as if they did
not. One more sort per declared pair in ANALYZE would give the number of distinct pairs, which
is enough to recognise a functional dependency.

**Build the reference and the random tests first.** The best decision in the project was
keeping the Bonus 1 engine as an in-memory reference: every module above it is tested by running
the same plan both ways. The second best was generating random plans, which found three
frame-accounting bugs in one operator that no hand-written test had the shape for. Both came
late. The storage engine's first version had only tests written against itself, and all of them
passed while it was wrong in nine places.

**Smaller things.** The hash join always makes B − 1 partitions, as specified, which is wasteful
when the input is small against B; it should size them from the input and skip partitioning when
the build side fits. A sort-merge join on heavily duplicated keys re-reads the duplicate run from
disk for every left row. Deleted space is never reclaimed. And the prompt opens the database
afresh for every statement, which keeps page counts reproducible and makes a long script of
INSERTs slow.
