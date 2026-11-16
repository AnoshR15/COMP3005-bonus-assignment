# DESIGN_LOG.md

One entry per working session. Each entry says what I was trying to do, what I tried, and what
broke. Paragraphs marked **AI mistake** are places where AI-generated code or text was wrong,
slow or incomplete, how I noticed, and what changed.

## 2026-09-26: repository structure and first grammar

**Goal.** Set up the repository layout and write the grammar before any parser code.

**Tried.** Wrote the first EBNF with the precedence levels decided: the three set operators on
one level, left associative; `times` tighter than them; `join` tighter than `times`; `not` over
`and` over `or` inside conditions. Sketched the recursive-descent structure as one function per
level.

**What broke.** Nothing ran yet. The open question was whether `intersect` should bind tighter
than `union` and `minus`, as it does in SQL. Decided against it so that a chain of set operators
always reads left to right, and wrote that into GRAMMAR.md.

## 2026-09-27 to 2026-09-29: first implementation, drafted with AI

**Goal.** Tokenizer, parser, tree printer, operators, loader, data generator, and the document
templates.

**Tried.** Asked for each component against the grammar and ran the 25 cases from section 7
through the command line.

**What broke.** The tokenizer and parser passed all 17 lexical and grammar cases: maximal munch
on `>=`, the `>-30` split, strings containing `)` and `,` and `''`, and the left grouping of
`A union B minus C`. The semantics cases also passed on the small examples. That turned out to
be misleading (see the next entry): the generated tests only asserted that a parse returned
something, and the small data never reached the corners.

**AI mistake (incomplete).** REPORT.md and DESIGN_LOG.md came back as templates full of TODO,
and the generated tests for cases 10 to 13 were `assertIsNotNone(parse(q))`, which passes for
any grouping at all. Found by rereading section 7 of the assignment next to the tests: case 10
says the printed tree must match GRAMMAR.md, and nothing checked the tree.

## 2026-09-30, session 1: review against the specification

**Goal.** Check every deliverable against sections 3 to 8 before doing the performance study.

**Tried.** Ran the 25 cases through `ra.py`, then wrote probe queries for the corners the cases
do not cover: empty inputs, short-circuit conditions, keyword names, missing files, and the
benchmark at 500, 1000, 2000 and 4000 tuples per relation.

**What broke.**

- **AI mistake (slow, then fatal).** `join` was written literally as `select(times(a, b), cond)`.
  It built the whole n x m product as a set of tuples, then filtered it. At n = m = 2000 it took
  33.5 s and 490 MB; at 4000 x 4000 it raised MemoryError under a 1.5 GB limit. The report needs
  64000 x 64000, which is 4.1 billion pairs. Found by running the benchmark, not by reading: the
  code looks exactly like the definition in section 4.3, which is the trap.
- **AI mistake (wrong).** The data generator's `--match-rate` had no effect. It gave S the key
  `i % (rate * n)`, so a small fraction of R matched many S tuples each and the output size was
  always exactly m. Found by running the join at rates 0.01, 0.1, 0.5 and 1.0 and getting 1000
  output tuples every time. Question 5 of the report could not have been answered with it.
- **AI mistake (wrong).** Attribute names and operand types were checked inside the per-row loop.
  `select[Nope=1](select[Age>1000](Employees))` printed an empty result with exit code 0, and
  `select[Age>1000 and Name>5](Employees)` never reported the number-to-string comparison
  because `and` short-circuited before reaching it. Found with probe queries after noticing that
  `resolve_attr` was being called from inside `eval_cond`.
- **AI mistake (wrong).** GRAMMAR.md said any keyword works as an attribute name.
  `select[not=3](R)` fails with "Expected identifier", because `not` is tried first in
  `not_condition`. Found by testing the sentence.
- **AI mistake (wrong).** The loader typed a column holding both `1` and `1.5` as a string. The
  expression was `float if ts=={float} else int if ts=={int} else str`, and a set holding both
  types fell through to `str`. Found by reading the type-inference lines while checking how union
  compatibility decides types.
- A missing data file produced a `FileNotFoundError` traceback: `ra.py` only caught the engine's
  own exceptions.
- The tree printer prefixed the root with a connector, and `project[Emp.Name]` printed as
  `attrs=[Name]`, dropping the qualifier.
- Slow: `resolve_attr` scanned the schema list for every attribute of every row. The 1000 x 1000
  join took 4.2 s.

## 2026-09-30, session 2: fixes

**Goal.** Fix everything from session 1 without making the code harder to explain.

**Tried and changed.**

- `join` now walks the product one pair at a time, evaluates the condition, and keeps only the
  matches. The comparison counter is incremented inside the inner loop, once per pair, whether
  or not it matches. A test checks the result equals `select(times(a, b), c)`.
- New `bind(cond, schema)` resolves every attribute to a column index and type-checks every
  comparison once, before the loop, and returns a plain function `row -> bool`. Name and type
  errors now surface for empty inputs and for the unreached side of an `and`. Side effect: the
  1000 x 1000 join dropped from 4.2 s to 0.22 s because the schema is no longer scanned per row.
- The two engine types are now `number` and `string`, inferred per column; the `else str`
  fall-through is gone.
- Tuple equality is written down as `tuple_equal`, and every stored row is put in canonical form
  (`30.0` becomes `30`), so the set container implements exactly that definition.
- Generator rewritten: match rate k means each R tuple joins with k S tuples on average.
  `round(k * n)` S tuples get partner keys spread over R's keys; the rest get keys above n. The
  output size is now k * n and the comparison count is unchanged, checked by a test at n = 200.
- `ra.py` catches file errors and any internal error as one line, and reports the counters on
  standard error after each run.
- `rename` checks for colliding attribute names. Parser errors say what was found, or "at end of
  query" when the input ended early.
- Tests rewritten: one test per required case that asserts the printed tree or the result set,
  plus tests for the five error categories, each fixed defect, the loader, the generator and the
  command line. 46 tests pass.

## 2026-09-30, session 3: performance study

**Goal.** Run the experiment at 1000 to 64000 tuples per relation and answer the six questions.

**Tried.** `python -m source.benchmark all`, which writes `results/join.csv`,
`results/unary.csv` and `results/match_rate.csv` and logs to `results/benchmark_log.txt`, then
`python -m source.plot` for the chart and the fitted slopes.

**Measured.** Join: comparisons exactly n x m at all seven sizes; 0.249 s at 1000 and 1336.7 s
(22 minutes) at 64000; fitted log-log slope 2.01. Select and project: linear, slopes 1.04 and
1.10, 0.018 s and 0.094 s at 64000. Match rate at n = m = 8000: 64,000,000 comparisons at every
rate and 12.2 to 13.0 s of wall time. All of it is in REPORT.md, copied from the CSV files.

**What broke.** Two things. The first match-rate pass read 15.6 to 17.8 s, well above the 12.2 s
of the 8000 row in the join table. It had run while I was executing the test suite and a plotting
dry run on the same machine. Ran it again alone and got 12.2 to 13.0 s; both passes are in the
log, the clean one is in the report. Second, the 64000 join took 6.6 times the 32000 time instead
of 4: 0.33 instead of 0.20 microseconds per comparison. One run could not tell me whether the
inner relation had outgrown the cache or a 22-minute run had been throttled or moved to an
efficiency core, so I ran the same 1.024 billion pairs both ways round, 16000 x 64000 and
64000 x 16000: 353 s with the large relation in the inner loop, 185 s with it outside, and the
slow one ran first. The inner relation's size is the cause; REPORT.md section 2 has the numbers.

**What I learned.** The comparison count is the algorithm; the time is the algorithm times a
constant the machine sets, and that constant moved by 1.9x on unchanged code between two sizes.
I would not have trusted the 64000 number without the swapped run. Also: time nothing while
anything else is running, and copy every number from a file rather than from memory.

## 2026-09-30, session 4: test layout

**Goal.** Make the tests read like the rest of the project: one test module per source module,
test data in files, no path tricks.

**Changed.** `tests/` is now a package run from the project root with `python -m unittest -v`.
`tests/base.py` holds one base class: a test class lists the relation files it needs in `files`
and gets them loaded once by `setUpClass`; the class wraps `parse`, `tree` and `evaluate` so a
test reads `self.rows(query)` or `self.schema(query)`. The relations the tests use live in
`tests/data/` as ordinary `.ra` files (`tests/data/bad/` has one malformed file per data error)
instead of strings written to temporary files. Cases 1 to 25 keep their number in each test's
docstring and `tests/required_cases.txt` maps every case to its test. Loops over several
queries use `subTest`, so a failure names the query. 48 tests pass.

## 2026-11-11: Project 2 week one, turning the engine into a library

**Goal.** The seven preparation points of Project 2 section 1.3, before any storage code: separate
layers, a standalone condition parser, qualified names everywhere, the tree as JSON, operators
detached from in-memory relations, bags instead of sets, and the Bonus 1 suite still passing.
Done with AI assistance; the paragraph marked **AI mistake** is where it was wrong.

**Changed.**

- *Layers.* `parser.py` no longer imports the tokenizer (it takes a token list), so tokenizer,
  parser, tree and operators each import without the others. `tests/test_layers.py` starts a fresh
  interpreter per layer and checks which package modules got loaded, and reads every library
  module's syntax tree to check it never calls `print`, `open` or `input`. `ra.py` is the only
  place that prints.
- *Conditions.* `source/condition.py`: `parse_condition(text)`, `bind(tree, schema)`,
  `compile_condition(text, schema)` and `condition_text(tree)`. The parser gained
  `parse_condition`, which is the existing `condition` rule followed by end of input.
  `condition_text` is new: plans carry conditions as strings, so a tree has to be written back as
  text that parses to the same tree. It writes parentheses only where a child binds looser than
  its parent, or sits on the right of the same left-associative operator.
- *Qualified names.* The resolver already handled `R.a`; nothing in it changed. What was missing
  was proof, so `tests/test_bags.py` now runs qualified references through select, project,
  rename, join, times and distinct, and checks that a wrong qualifier is a name error.
- *JSON.* `source/plan_json.py`, with `to_json`, `from_json`, `dumps`, `loads`. A leaf is
  `{"op": "scan", "table": "R"}`, matching the plan example in the handout. `from_json` checks
  every node against a table of required fields and types and names the node in the error. The
  round-trip test covers all ten node types, both `all` values, and goes through real JSON text.
- *Data source.* Operators take a `Relation` whose `rows` is any iterable and return one whose
  rows are a generator. `evaluate(tree, source)` asks `source.scan(name)` at each leaf.
  `MemorySource` wraps the loaded relation files. Schema, name and type checks still happen when
  the operator is called, not when the first row is pulled; a test with a counting source shows
  that building a plan reads zero rows and that pulling one result row reads only as far as needed.
- *Bags.* `project` keeps duplicates. `distinct(e)` is a new operator and new syntax. The loader
  keeps a tuple that is written twice. `union`, `intersect` and `minus` stay set operators (their
  results have no duplicates), and each has a bag version behind `keep_all=True` for UNION ALL
  and friends: m + n copies, min(m, n) copies, max(m - n, 0) copies.

**Tests that changed meaning (point 7).** Two of the 25 cases, both because of bags:

- Case 23, `project[DID](Employees)`: was `{D1, D2}`, is now `D1, D1, D2`. The test asserts that,
  and that `distinct(project[DID](Employees))` is `{D1, D2}`.
- Case 19: the join is unchanged, but its follow-up `project[Dept.DID](...)` expected `{D1, D2}`
  and now returns `D1, D1, D2`.

Outside the 25, `test_duplicates_collapse_on_load` became `test_duplicates_are_kept_on_load`, and
the generator test and the benchmark pass a `MemorySource` and materialise the result, which is an
interface change, not a change of meaning. The other 23 cases pass with their assertions
untouched. To make that claim mean something, `rows()` in `tests/base.py` now fails when a result
contains duplicate rows instead of quietly turning it into a set. 84 tests pass.

**What broke.**

- Running the old suite straight after the operator rewrite gave exactly four failures and one
  error: cases 19 and 23, the two loader assertions that compared against a set, and the
  generator test that passed a dictionary where a source is now expected. Nothing unexpected,
  which is the point of running the old tests before touching them.
- **AI mistake (quietly incomplete test).** The JSON round-trip test was written with one large
  query, labelled `everything`, that nests seven operators. The loop that feeds the test had a
  leftover `if name != 'everything'` filter, so the biggest tree was never round-tripped and the
  suite still printed OK. Found by rereading the test after it passed on the first run, which was
  itself the reason to distrust it. The filter is gone; the query is skipped only in the test
  that executes plans, because it names columns that do not exist.

**Still held in memory, on purpose.** `join` and `times` copy their right input into a list,
because a generator can be walked once and the inner side is walked once per outer row. `distinct`
and the set operators keep a set of rows seen. These are the Bonus 1 algorithms behind a new
interface. Module 2 replaces them with block nested loop, external sort and Grace hash under a
page budget; week one only had to remove the dependency on relations being lists.

**Not rerun.** REPORT.md's numbers come from the Bonus 1 engine. Comparison counts are the same
(a quick run at n = 500 and 1000 gave n x m again), but `project` no longer deduplicates, so its
timings in that report describe the old code.

## 2026-11-11: Module 3, SQL front end

**Goal.** SQL in, logical plan JSON out, to the Module 3 specification: lexer, parser, name
resolution, aggregate and type rules, translation in the canonical order, three commands, the
fifteen required cases. Done with AI assistance; **AI mistake** marks where it was wrong.

**Tried.** A new package `sqlfront/` beside the Bonus engine instead of stretching the Bonus
parser: SQL has line and column positions, case-insensitive keywords and a different token set.
What is shared is the condition layer. A resolved SQL condition is rebuilt from the engine's
`Compare` / `Logic` / `Attr` / `Literal` nodes and printed by `condition_text`, so the strings in
a plan come from the same code that reads them back. Analysis and translation are one pass in
canonical order (FROM, WHERE, GROUP BY, HAVING, SELECT, ORDER BY, LIMIT, set operators).
Details in DESIGN_M3.md.

**What broke.**

- **AI mistake (wrong guesses in the contract).** In week one the plan JSON was written before
  this specification was read, and three names were guessed: `rename` carried `"name"`, the
  product was `"times"`, and UNION ALL was `{"op":"union","all":true}`. The specification says
  `"to"`, `cross` and a separate `union_all` operator. A structural comparison of plans would
  have failed on every aliased table. Found by reading Section 5.2 of the specification beside
  `plan_json.py`. Fixed in `plan_json.py`, which now also knows `group_by`, `sort` and `limit`.
  The lesson is the one the handout states: read the module document before writing the format.
- **AI mistake (a Bonus rule that contradicts SQL).** The engine's union compatibility check
  required the two inputs to have the same column *names*. SQL requires the same count and
  types only, and `SELECT name FROM Emp UNION SELECT title FROM Dept` is legal. The translator
  would have accepted the query and the plan would then have failed when run. Found while
  writing required case 14 and asking what exactly "schema error" should cover. The engine's
  check now matches the specification; the two Bonus tests on it still pass because they fail
  on count and on type.
- The specification's grammar sketch has no aggregate among the operands, but its own example
  writes `HAVING COUNT(*) > 5` and case 7 wants an aggregate error, not a syntax error, for
  `WHERE COUNT(*) > 1`. The grammar had to differ from the sketch here (GRAMMAR_SQL.md 3).
- ORDER BY on a column that is not selected (case 9) does not fit "project, then sort": the
  sort would name a column the project just dropped. Settled on project wide, sort, project
  narrow. This is a judgement call and is listed as one in DESIGN_M3.md section 8.
- "Do not emit distinct above GROUP BY" is only safe when the grouping columns are selected.
  `SELECT DISTINCT COUNT(*) FROM Emp GROUP BY did` needs the distinct; with it removed, two
  departments of equal size give two equal rows. The rule is conditional and a test runs both.
- The first full run of the new SQL tests passed without a single failure, which is not evidence
  that they test anything. Checked by breaking the DISTINCT rule on purpose: one test failed, as
  it should. A second deliberate break (taking the last candidate instead of the first in
  `resolve`) changed nothing, because at that line there is exactly one candidate; that was a
  useless mutation, not a passing check, and is recorded as such.

**Not decided by the specification, decided here.** The field name of `limit` (`count`), how
`SELECT a AS x` appears in a project (`["Emp.a","x"]`), what `compile` prints for CREATE and
INSERT, and the catalog file. The catalog is a stand-in: Module 1 is not built yet, so `--db DIR`
reads `DIR/catalog.json` behind a three-method interface.

**Changed in the Bonus engine for this module.** The tokenizer reads `<>`. `project` accepts
`(attribute, new name)`. New in-memory `group_by`, `sort`, `limit`, so that plans can be run in
tests; they are reference versions, not the Module 2 operators. 145 tests pass.

## 2026-11-11: Module 2, execution engine, and the storage layer under it

**Goal.** Run a physical plan inside B buffer pool frames: the iterator interface, external
sort, three joins, group_by and distinct in two modes, the set operators, counters per operator,
the two commands, the five measurements. Done with AI assistance; **AI mistake** marks where it
was wrong.

**Order of work, and a risk taken.** Module 2 runs on Module 1 and Module 1 did not exist. Its
specification was not to hand either, so the storage layer (`store/`) was built to the contract
in the project overview: disk manager, slotted pages, heap files with a free space directory,
buffer pool with LRU and CLOCK, catalog in pages. The byte layout is my own. If the Module 1
document fixes a different one, `store/page.py` and `store/record.py` change and the executor
does not, because it only uses the contract. This is the reverse of the order the handout
recommends and it is recorded here as a risk, not as a finished Module 1: there is no
DESIGN_M1.md or REPORT_M1.md, and the trace command has not been checked against the published
traces.

**Tried.**

- All memory is buffer pool slots. A table page is a pinned slot; working memory is a reserved
  slot. Rows kept in a Python list go through a `Workspace` that reserves first and refuses a
  row that does not fit. The budget is then a property of the pool, not a promise of each
  operator.
- Dividing B: first idea was one number per operator. It cannot work at B = 4, where a sort above
  a join would need three frames each. What works is two numbers, `frames` (during open) and
  `limit` (while streaming), because a blocking child finishes its heavy work before its parent
  starts. Three rules, DESIGN_M2.md section 2.
- When even that is impossible, the child's output is spooled to a temporary file. That is a
  materialisation the algorithm did not ask for, so it happens only when the frames force it.

**What broke.**

- **AI mistake (did not reproduce the specification's example).** The first sort merged F − 1
  runs with runs of F − 1 pages, since the scan below it holds one frame. On the 8-page example
  that gave 16 reads and 8 writes at 4 frames and 24 and 16 at 3 frames, neither of which is the
  22 and 14 of the specification, and no choice of B made it match. Found by running the example
  before writing any test for it. The fix is not a bigger workspace, which would break the
  budget: when the workspace fills, the row that did not fit and the rest of its page are still
  in the scan's frame, so the sort takes that frame over. Runs are then B pages and the example
  reproduces exactly at B = 3.
- **AI mistake (over the budget, found by a test).** `union_all` opens its right input when the
  left one is finished. Every other operator opens its inputs before its parent has reserved
  anything, and the frame arithmetic assumed that everywhere. A hash join on the right of a
  union_all, under a hash distinct, opened with "4 frames" while the distinct above already held
  one: `BufferPoolError` at B = 4. Found by the plan "distinct over union over joins" in the
  every-plan-at-B-4 test. The pool raised instead of quietly using a fifth frame, which is the
  only reason this was a failing test and not a wrong claim in the report.
- **AI mistake (over the budget again, found only by random plans).** The first fix gave the
  right input `limit` frames but still declared that union_all *holds* only what its inputs hold
  while streaming. The right input's late `open()` uses up to `limit`, which is more, so the
  parent sized its workspace too large. It failed at 8 and 23 frames and passed at 4 and 5. No
  hand-written test had that shape at those sizes. Found by generating random plan trees and
  comparing each with the Bonus 1 engine at four budgets. The same search found that a
  union_all with a heavy right input could not run at 4 frames at all; now that input is
  spooled during `open()`.
- **AI mistake (deleted its own input).** The hash group_by's last resort sorts a spill file.
  `Sort.open()` begins by closing itself, which closed its input, and the input was written to
  delete its file on close. The file was gone before the first read. The path is only reached
  after three rounds of partitioning fail, so nothing exercised it. Found by a test that sets
  `MAX_LEVEL = 0` to force it.
- **AI mistake (quietly expensive, kept on purpose).** The hash join always makes B − 1
  partitions, as specified. At B = 256 on a 20-page input that is 255 files of one part-filled
  page each: 632 page operations where the formula says 90. Found in the cost table, not by a
  test, since the rows are right. Left as specified and explained in REPORT_M2.md section 1.
- **AI mistake (a test that did not test).** The first skew test joined rows of one key and
  asserted that the last overflow action was a nested loop. The rows were 10 bytes wide, the
  whole partition fit in one page, and there was no overflow at all; the assertion failed on an
  empty list. Replaced by a table of wide rows where one key fills more than three pages.
- Two expectations in tests were guesses written before running anything (a hash value and a
  JSON column number). Both were wrong and were replaced by the observed values, which is fine
  for a regression pin and would not be fine for anything else.

**Measured.** REPORT_M2.md. Block nested loop equals its formula in all twelve cases. Sort runs
and passes equal the formula in all ten. Sort-merge reads are below the formula because the
merge stops when one input ends. The hash join matches 3 (M + N) only in the middle range of B.

**What I learned.** The error path of the buffer pool was the most useful code in the module.
Three budget bugs showed up as exceptions with a stack trace instead of as numbers that looked
plausible. And hand-written tests cover the plans I thought of; the bug was in one I had not.

## 2026-11-11: Module 1, storage engine, rebuilt to its specification

**Goal.** Replace the storage layer written earlier from the overview handout with one that
follows the Module 1 specification byte for byte, without breaking the execution engine built on
top of it. Done with AI assistance; **AI mistake** marks where it was wrong.

**What the earlier layer got wrong.** It was written before the specification was read, which
the handout warns against, and it had been recorded in this log as a risk. The bill:

| | Earlier layer | Specification |
|---|---|---|
| files | one `data.db` for everything | one file per table, plus `catalog.db` |
| page header | 12 bytes: next page, slot count, free end, deleted slots, garbage | 8 bytes: page id, slot count, free end |
| VARCHAR | 1 length byte | 2 length bytes |
| deleted slot | reused by the next insert | never reused |
| compaction | automatic, inside insert | optional; delete frees nothing |
| data pages | chained by a next-page pointer | not chained; listed in directory pages |
| scan | follows the chain, 1 page pinned | follows the directory, 2 pages pinned |
| CLOCK | hand moved only when choosing a victim | hand also moves past each newly loaded page |
| CSV | a first line of column names was skipped | no header line |

Every one of those would have failed a comparison of counters or bytes. None would have been
caught by the tests of that layer, which all passed, because they tested it against itself.

**What survived, and why.** The executor reaches storage through `fetch_page`, `unpin_page`, the
catalog, `page.records` and the record codec. Replacing everything under those names changed
one operator (`SeqScan`) and two lines of `RunWriter`. All 88 executor and SQL tests passed on
the new layer before any of them was touched, except one that had counted catalog page reads.

**What broke, and what had to be decided.**

- **AI mistake (a design that leaks, caught before it was built).** The first catalog stored
  table entries as records in slotted pages and updated one by deleting and re-inserting. Under
  this module's rules a delete frees nothing and a slot is never reused, so every save of a
  table's row count would have left the old entry behind and the catalog would have grown with
  every load. Found by walking the old update path through the new page rules before porting
  it. The catalog now has its own page format in which an entry is rewritten in place
  (DESIGN_M1.md section 7), and a test saves one entry fifty times and checks the catalog is
  still one page.
- **Two specifications that disagree, resolved by a rule about the file.** Module 1 lets a scan
  pin a directory page and a data page. Module 2 says a table scan holds one frame and reads M
  pages, and its whole frame arithmetic depends on that: at B = 4 a nested loop join over two
  scans would need 5 frames if each scan held 2. Unpinning the directory between data pages
  keeps it to one frame but doubles the reads when the pool is full, which it always is under a
  join. The way out is that pages are only added at the end of a table file, so the file is
  always directory, 681 data pages, directory, and so on; the executor's scan walks page ids and
  skips multiples of 682, without reading a directory. `storage scan` still follows the
  directory. The rule is an invariant I now depend on, so a test builds a table with two
  directory pages and compares what they list with what the rule predicts.
- **First fit, done literally, is quadratic.** The specification's insert walks the directory
  from the front for every row. Loading 500 pages that way looks at about 30 million entries.
  The fix keeps first fit exact: free space never grows (no compaction), so entries already
  found too small for a record of some size stay too small for any record at least that long,
  and the insert may start after them. That argument is the reason I did not implement
  compaction casually: it would silently break the hint.
- **AI mistake (a test that was wrong, not the code).** The first-fit test expected a 100-byte
  record to go into a page with 98 bytes free. The heap file correctly put it in the next page
  and the test failed. The arithmetic in the test was mine and was off by the 4-byte slot of the
  previous insert.
- **AI mistake (a measurement that was wrong, not the engine).** The capacity table first
  reported that the fullest page of the VARCHAR(200) table held 65,535 rows, and a mean of 81
  where the answer is 19. The measuring script read every page of the file as a data page, and
  page 682 is a directory page whose bytes 4 and 5 are `FF FF`. Found because the number is
  absurd; a number wrong by ten percent would have gone into the report. The script now asks the
  heap file which pages are data pages.
- **CLOCK was right on the worked example for the wrong reason.** The earlier pool never moved
  the hand when a page was loaded into an empty frame. On the example it gave the right totals
  anyway, because after exactly B loads the hand "should" be back at frame 0, where it had never
  left. It would have differed as soon as frames were freed in the middle of a run. Now the hand
  moves past every newly loaded page, as specified.

**Not done.** The five published traces were not available, so section 2 of REPORT_M1.md is
measured on generated stand-ins and says so in every row. Trace 3 is generated as the
specification describes it and its rows are final. The script reads the real files from
`traces/` when they are there.

**Checked afterwards.** All Module 2 measurements were run again on the new layer. Every counter
in every CSV file was identical to the earlier run; only wall-clock times moved. Rows per page
did not change for the tables involved (255 for three INTs, 19 for the wide rows), and the
executor counts pages.

**What I learned.** The contract protected the module above and nothing protected the module
itself. Tests written against my own design could only tell me that I had built what I had
designed.

## 2026-11-11: Module 4, query optimizer

**Goal.** Logical plan in, physical plan out: ANALYZE with equi-depth histograms, selectivity,
seven rewrite rules, a page I/O cost model, dynamic programming over left-deep join orders, four
commands, four measurements. Done with AI assistance; **AI mistake** marks where it was wrong.

**Not available.** The twenty supplied queries and the supplied dataset. REPORT_M4.md is measured
on a generated stand-in (six tables, twenty queries, three correlated column pairs) and says so
at the top. The numbers have to be redone on the real workload; `bench/m4_measure.py` is built
for that.

**Tried.**

- ANALYZE as one scan that splits the table into a temporary file per column, then one external
  sort per column. The first thought, a sort of the table per column, reads the table once per
  column.
- Selectivity as specified, plus one change that turned out to matter: a filtered column keeps
  a histogram cut down to what passed, so two range predicates on one column are an interval and
  not a product.
- Rules 1 to 5 as one "sink each predicate as deep as it goes" pass, then rule 6, then rule 7.
- The cost model with the convention of the specification's own example, where a formula's M
  term is the reading of the input: a table scan is not charged twice, an intermediate result is.
- `--assume`, to plan with estimates supplied by hand. Written to reproduce the DP table of
  Section 5.1, it became the tool for the last measurement: plan, run, read the true row count,
  plan again told the truth, run that.

**What broke.**

- **AI mistake (invalid plan from a rewrite).** Rule 6 inserted a projection of *no columns*
  under `COUNT(*)` over a join: nothing above needed any column of that input. Found by the test
  that runs every query's rewritten plan against the original with each rule switched off; it
  appeared only with rule 2 off, when the select sat between the group_by and the join. A
  projection now always keeps at least one column.
- **AI mistake (the same query, two plans).** A query written with commas and with JOIN ON gave
  plans that differed in one string: `w.eid = e.eid` against `e.eid = w.eid`. The nested loop
  join copied the condition as the user spelled it. Found by the checklist test, on the
  four-table query only; the two- and three-table ones happened to be spelled alike. Equalities
  are now always written outer column first and predicates are ordered by a spelling-independent
  key.
- **AI mistake (a rule that could never fire).** Rule 4 as specified pushes a select below a
  rename. Every subquery Module 3 produces is a rename over a *projection*, and nothing moved a
  select past a projection, so a predicate on a subquery stopped at its select list and rule 5
  never saw it. Found when a test of rule 5 expected the predicate below the group_by and got it
  above the projection. Rule 4 now also passes a projection, rewriting renamed columns to their
  sources.
- **AI mistake (a report with a hidden second cause).** The search for a bad plan caused by
  correlated columns found one: 610 pages against 331. The first draft attributed all of it to
  the independence assumption. Reading the executor's trace of the chosen plan showed a join
  predicted at 64 pages doing 495 reads, which is 33 passes over a 15-page inner. The estimate
  explains the 15 (8 were expected). It does not explain 33 passes where 7 were expected: that is
  the executor giving the lower of two pipelined joins a block of one frame, which the cost model
  knows nothing about. The report now separates the two. Found by dividing 495 by 15.
- **AI mistake (ANALYZE under-reported itself).** Its I/O summary left out the scan of the table,
  because the scan operator counts on its own counters. Found by a test asserting that reads
  minus writes equals the table's pages.
- **Tests that could not show what they claimed.** "Changing B changes the join algorithm" was
  first written on tables of three pages, where a nested loop wins at every B. And I expected a
  hash join at B = 8 on 30 and 20 pages; the model says 130 for the nested loop against 150, and
  the model was right. The test data got two 20- and 30-page tables, and the expectation was
  replaced by the arithmetic.
- **The specification's table has a row that does not follow its formula.** In 5.1 the
  alternative for {A, B', C} is written 410 + 40 + 100 = 550. With 40 pages of outer and B − 2 = 30,
  the formula reads the inner twice: 650. My table prints 650 and DESIGN_M4.md says so. The
  winner, 530, is the same.
- **A q-error of 19.9 that is not an error.** A sort under `LIMIT 10` was estimated at 199 rows
  and the executor counted 10, because the limit stopped pulling. Kept in the table and
  explained, not filtered out.
- **Joins come out in another order than written**, and so do their columns. Harmless for
  anything that names columns; wrong for a set operator or the final result, which go by
  position. Caught while writing the planner, before any test: a projection restores the order
  where position matters, and a test with a union of two reordered joins guards it.

**Measured.** On the stand-in, at B = 8: median q-error 1.00 over 200 nodes, worst 19.9; the
optimizer does 6.5 times less I/O than no optimizer; rules 6, 2 and 1 carry all of the rewrite
benefit and rules 3, 4, 5 and 7 change no I/O at all, each for a reason the report gives; the
correlated dates cost a factor of 1.84 in the worst case found.

**What I learned.** The measurement I trusted least, the one built to make the optimizer fail,
was the one that taught me something about a different module: the frame split of Module 2 is
invisible to the cost model of Module 4, and I only saw it because I asked why one number was
495.

## 2026-11-11: Integration

**Goal.** One prompt over the four modules: the SQL subset plus EXPLAIN, EXPLAIN ANALYZE,
ANALYZE, SET frames and LOAD, with the four module tools still working. Done with AI assistance.

**Tried.** `minidb.py` calls `compile_sql`, `Planner.optimize` and `Execution` in that order and
prints. CREATE TABLE and INSERT use what Module 3 already emits for them. Every statement opens
the database with a buffer pool of its own, so a query's page counts are those of `exec run` on
the same plan and do not depend on what ran before. `--report` prints one line per query with
the figures the hidden workload asks for.

**What broke.** Nothing in the modules: the nine integration tests passed on the first run,
which for once I believe, because every step of the path already had a test of its own that
went through the same functions (`opt plan` into `exec run`, SQL into the reference engine).
Two decisions were needed. EXPLAIN is not SQL, so the prompt removes the word before handing the
rest to Module 3; it replaces it with spaces rather than cutting it, so that an error still
reports the line and column of the text the user typed. And a semicolon ends a statement only
outside a string and outside a `--` comment, which a test checks with `'Sales; and more'`.

**Not done.** The hidden workload, the PostgreSQL comparison and the demo. ARCHITECTURE.md
section 4 gives the two commands to run when the data arrives.

## 2026-11-13: the handout's command lines, typed as printed

**Goal.** Check the finished system against the overview handout line by line, by running every
command it prints exactly as it prints it. Done with AI assistance; **AI mistake** marks where
it was wrong.

**Tried.** A script that builds a database in `./db` and runs the fourteen command lines of
sections 2.3, 3.3, 4.3 and 5.2 with the handout's options and no others, then the bad plans of
section 3.2 and a script through the prompt.

**What broke.**

- **AI mistake (tools that only ran the way their own tests called them).** Six of the fourteen
  were usage errors. `exec run --plan plan.json --frames 32 --json` stopped with "the following
  arguments are required: --db", because `exec.py` required a flag the handout never gives, while
  `storage.py` already defaulted it to `db`. All four `opt` commands rejected `--catalog db/`:
  `sql.py` accepted both spellings and `opt.py` only `--db`. And `opt dp-table` required
  `--frames`, which the handout leaves off that one command. None of the 292 tests saw it,
  because every command-line test passed `--db` and `--frames` itself. The tests agreed with the
  code and neither agreed with the document the grader types from.
- Fixed in one place per tool: `--db` and `--catalog` are one option in all five tools, the
  directory is `db` when neither is given, and `--frames` of `opt plan`, `explain` and `dp-table`
  is 32 when not given. `tests/test_handout_cli.py` now runs the handout's lines from a directory
  holding `./db`, which is the test that should have existed from the first module.
- Nothing else differed. The three example messages of section 3.2 come out as
  `Node 'sort' at $: missing required field 'keys'`, `Node 'filter' at $: field 'condition' must
  be a string` and `Node 'merge_join' at $: unknown operator 'merge_join'`; malformed JSON reports
  its line and column; an unknown table or column is rejected before any page is read.

**Still open.** The measurements of REPORT_M1.md section 2 and all of REPORT_M4.md were taken on
generated stand-ins and say so. They have to be run again on the published traces (into
`traces/`) and on the supplied dataset and twenty queries. Nothing is committed or tagged yet.

## 2026-11-13: Module 1 checked against its specification a second time

**Goal.** Not to trust the first check. Re-read the Module 1 document section by section and
confirm each required number by running the tool, with the expected values written from the
document and not from my code. Done with AI assistance; **AI mistake** marks where it was wrong.

**Tried.** A script outside the project that holds its own copy of Section 5, the buffer pool
pseudo-code typed in literally for both policies, and compares it with `storage trace` on 800
random traces (uniform, cyclic, hot set, working set interrupted by scans; B from 1 to 64).
Then the record example of Section 2, the page example of 3.2 step by step through
`storage layout`, 240 records on a page for L = 13, the five errors of Section 9, the JSON
shapes of Section 7, and the checklist of Section 10 on a 500-page table at B = 2, 8 and 64.

**What held.** All of it. The reference and the tool agree on hits, misses, evictions, reads
and writes on every trace. The page dump in DESIGN_M1.md section 3 is byte for byte what a
fresh database writes. The scan reads 502 pages at every B and pins two.

**What broke.**

- **AI mistake (the rule kept in the engine and broken in the command above it).**
  `HeapFile.scan` holds one data page at a time, as Section 4.1 demands, and its test passes.
  But `storage scan` appended every row it received to a Python list and printed the list at the
  end, so the command held the whole table in memory anyway, outside the pool where no counter
  sees it. The specification says a scan that reads the table into a list fails the module "no
  matter what it returns". The counters were right, which is exactly why nothing caught it: page
  reads and pins measure the pool, and the list was not in the pool. I found it by reading the
  command for where its rows go, after the engine had passed everything.
- Fixed by writing each row as it arrives, the way `exec run` already did. The output is the
  same bytes (nine outputs compared by hash before and after), and peak memory for the 500-page
  scan went from 45 MB to 17 MB. The new test would have failed on the old code: it checks that
  three pages have been read when the first row is written, and the old code had read seven.

**What I learned.** A budget enforced by a counter only covers what the counter can see. The
same question has to be asked of every layer that receives rows: where do they go next.

**Still open for this module.** The five published traces. Section 8 wants its policy table
from them, and REPORT_M1.md section 2 is still on stand-ins.

## 2026-11-15: Module 2 checked against its specification a second time

**Goal.** The same as for Module 1: every required behaviour confirmed by running `exec`, with
the expected answers computed outside the project. Done with AI assistance; **AI mistake** marks
where it was wrong.

**Tried.** A script that builds seven small tables through `storage`, computes every expected
result in plain Python from the CSV data (joins by two nested `for` loops, groups by a
dictionary, set operators by Python sets), and compares bags with `exec run --json` at B = 4, 5,
8, 10, 16, 32 and 64. It checks the checklist of Section 6, the formulas of Section 2 that are
exact (block nested loop reads and comparisons, the sort example, sort passes at 20 × B), the
edge cases of group_by, the type and naming rules of 1.3, eleven malformed plans, and that no
temporary file is left after any run. A second script re-ran eleven rows of REPORT_M2.md.

**What held.** 131 checks, and all eleven report rows to the digit. The one check that failed
was my own: a one-column CSV with an empty string is a blank line, which the loader skips, as
the csv reader does. Written as `""` it loads, and the empty string sorts first.

**What broke.**

- **AI mistake (half a JSON object, then an error).** `exec run --json` writes rows as they
  arrive, which is right, and wrote the opening `{"schema": ..., "rows": [` before pulling the
  first row, which is not. The specification makes SUM over an empty input with no grouping
  columns an error. That error is raised when the plan runs, so the output was the opening of a
  JSON object followed by `error: ...` on the same line: not JSON, and not the one-line error
  the tool promises. No test saw it because the test of that error called the engine, not the
  command. Found by the outside script, which only looks at what the command prints. Fixed with
  `Execution.started()`, which opens the plan and fetches the first row before anything is
  written; the prompt had the same fault with its header line and has the same fix.
- **AI mistake (the tool disagreed with the only example it was given).** Section 4 shows a
  trace of a hash join over two 24-page scans: `reads=96 writes=48` on the join, `reads=24` on
  each scan. `exec trace` printed 48 on the join, each operator's own count, and offered the
  specification's numbers behind `--subtree`. The design document even said so. I had read
  section 1.4, "attributed to this operator", as settling it, and treated the example as the odd
  one out. It is the other way round: the example is the only place the output is defined, and
  96 + 48 is exactly 3 (M + N), the formula of 2.9, which counts reading the inputs as the
  join's work. The default now includes everything below a line, as the example does, and
  `--own` gives the other numbers. `exec run` totals never depended on this.
- **Not documented, and I could not have answered it.** Section 1.3 requires VARCHAR order to be
  byte-wise on UTF-8. The code compares Python strings, by code point. They are the same order,
  but DESIGN_M2.md did not say so and no test showed it. Both now exist.

**Left as it is, on purpose, until Module 4 is checked.** The inner input of a nested loop join
is spooled to a temporary file when it is anything but a plain scan, so a filtered inner costs
writes, where the formula of 2.7 has none and section 3 calls the operator non-blocking. Opening
a filter over a scan again for every block would match the formula. Which is right depends on
what the optimizer's cost model assumes for the inner side, so it waits for that document.

## 2026-11-15: Module 3 checked against its specification a second time

**Goal.** This module is graded by comparing my plans with the grader's, node for node, so the
check is the same comparison: every plan the specification prints, typed in by hand from the
document, against what `sql compile` prints. Done with AI assistance; **AI mistake** marks where
it was wrong.

**Tried.** A script that makes the catalog the way a grader has to, with `storage create-table`
(my catalog page format is my own, so nobody can hand me a `catalog.db`), then compiles each
example of Section 5 from a file and compares the JSON with the typed-in plan. Then the lexical
rules of Section 1, both precedence tables, the bag rules of Section 4, the fifteen cases with
their error categories, and the checklist of Section 9 as a walk over twenty plans. Each of the
twenty was then turned physical by `opt plan` and run by `exec run`, and thirteen results were
compared with rows computed in Python. A second script sent awkward names and literals down
the same path: a table called `Project`, columns called `project`, `rename`, `times` and
`minus`, which are keywords of my Bonus Project 1 language and not of SQL, strings containing
`'`, `"`, `--`, `;`, `and` and `x = 1 or y`, and the two ends of the INT range.

**What held.** Every Section 5 example is equal to the document's plan, field for field. All
fifteen cases. `<>` stays `<>` in the plan. Every plan ran, and every row matched. All the
awkward names and literals worked, which I did not expect of the keyword names: the condition
strings are written by `condition_text()` and read back by `parse_condition()`, the same module
both ways, and that module already accepted its own keywords as attribute names.

**What broke.**

- **AI mistake (an error message that quoted half the mistake).** `WHERE age > 1.5` was reported
  as `invalid number '1.'`. The scanner stopped at the first character that could not continue
  an integer and quoted up to there. It now quotes the whole token, `'1.5'`, `'12abc'`.
- **Not a bug, but I had not known it.** `create-table project` failed after `create-table
  Project` with "a file named project.db is already in the database directory". Identifiers are
  case sensitive and the catalog treats the two names as different; the file system I develop on
  does not. The specification fixes the file name as `<table>.db`, so there is nothing to change,
  and the refusal is the right behaviour. It is now a paragraph in DESIGN_M1.md.

**Three places where the specification shows no JSON and I had to choose** (DESIGN_M3.md
section 8). A plan compared structurally can lose marks on each, so they are listed here to ask
about before the checkpoint: the field of `limit` is `"count"`; ORDER BY on a column that is not
selected is a wide project, the sort, and a narrow project above it; and DISTINCT above GROUP BY
is left out only when every grouping column is still in the select list.

## 2026-11-15: Module 4 checked against its specification a second time

**Goal.** As for the other three: the statistics recomputed outside the project, the three
worked examples typed in from the document, the costs recomputed from Section 4 by hand, and
all of it compared with what `opt` prints. Done with AI assistance; **AI mistake** marks where
it was wrong.

**Tried.** A script that loads a 5,000-row table with a skewed column and a string column,
runs `opt analyze`, reads the statistics back out of the catalog and compares them with values
computed from the CSV: counts, widths, distinct values, and every histogram bucket recounted
from its own range. Then the worked example of Section 2 as real data (10,000 employees), the
rewrite example of Section 3 through `opt explain --rewrite`, the DP table of 5.1 through
`--assume`, and the checklist of Section 8. For the cost model, an 80-page and a 20-page table
joined at B = 4 to 128, with the predicted cost of the chosen plan beside what `exec run` did.

**What held.** Every statistic, every bucket. ANALYZE twice leaves `catalog.db` the same bytes.
The DP table. Commas and JOIN ON give one physical plan for two, three and four tables. The
join changes from hash to nested loop between B = 8 and B = 16. A plan over tables with no
statistics runs and is right. And the prediction against the measurement: sort exact at B = 8,
16 and 100; nested loop exact at every B, 240 and 240, 180 and 180, 160 and 160, 140 and 140,
including the plans where the inner input is spooled; hash 496 against 542 and 298 against 312,
the difference being the part-filled last page of each partition.

**What broke.**

- **AI mistake (an example that reproduced by luck).** DESIGN_M4.md said the rewrite example
  "reproduces line for line", and its test passed. My script used an Emp whose columns are
  declared `eid, did, age, name` and got `project [e.did, e.name]` where the specification has
  `project [e.name, e.did]`. Rule 6 listed the kept columns in the order of the table's
  columns. In the test's Emp, `name` is declared before `did`, so the two orders were the same
  and the test could not tell. The rule's sentence gives the order itself: the columns the
  ancestors need, then the ones the operator needs. Rule 6 now carries an ordered list instead
  of a set, and a second test uses a table where the join column is declared first.
- **AI mistake (a number that was right and read wrong).** `opt explain` printed for each node
  its share of the plan's cost: its subtree minus its inputs. A nested loop join over two scans
  that fits in one block has the formula M + N, both already on the scan lines, so the join's
  line said `est_cost=0`. The specification's example has `est_cost=0` on the filter, the page
  counts on the two scans and a large number on the join, which is its formula. That is the same
  disagreement with the only example given as in `exec trace` (the Module 2 entry above), and it
  gets the same answer: the default line is now the operator's own formula, and `--own` and `--subtree` print
  the other two numbers. `EXPLAIN ANALYZE` in the prompt compares subtree with subtree.
- **A finer histogram gave a worse estimate, and the rule is the cause.** The worked example
  of Section 2, loaded as data and analyzed with one bucket, gives the document's four numbers
  exactly: 200, 2,000, 40, 10,000. With the default twenty buckets, `age > 60` is estimated at
  2,058 and the true count is 1,960. Each age is 196 rows, so a bucket closes after three ages,
  59 to 61, and the linear fraction of that bucket above 60 is one half where one age of three
  qualifies. Kept as specified, since the document's own example is computed that way, and
  written up in DESIGN_M4.md section 2 with the fraction that would fix it.

**Four of my own checks were wrong before the tool was.** `10000 * 0.0396` is
396.00000000000006 in floating point and `ceil` of it is 397; the tool guards its rounding
against exactly that and said 396. A sort of 80 pages at B = 4 was measured at 544 against a
formula of 560: a carried run, already explained in REPORT_M2.md. A join I expected to change
algorithm with B stayed a nested loop at every B, because the query selected two narrow columns
and rule 6 had shrunk 80 pages and 20 pages to 3 and 2. That one taught me to read the DP table
before deciding the optimizer was stuck.

**Settled: the spooled inner input of a nested loop** (left open in the Module 2 entry). The
cost model defines N as the estimated pages of the *input*, which for a filtered table is the
filtered size. Only an input that has been written down can be read again at that price, the
planner charges for the spool explicitly, and the prediction equals the measurement. It stays.

**Still open.** REPORT_M4.md is measured on generated stand-ins. The twenty supplied queries
and the supplied dataset, with its correlated pair, replace them.

## 2026-11-15: the files that were never going to be posted

**What happened.** For two days three reports carried a note saying the published traces, the
sample plans and the JSON schema "were not available when this was written", and the tools were
built to pick the real files up when they appeared. I asked. The answer: those files will not be
posted, write your own traces and sample plans from the examples in the specifications, any
reasonable assumption is fine, state it in one line in the design document.

**What changed.**

- The five traces are now files, `traces/trace1.txt` to `trace5.txt`, written by
  `bench/m1_measure.py` with a fixed seed, plus `traces/example.txt`, the six fetches of Section
  5.3. The measurement reads the files it wrote, so a row of the policy table is what
  `storage trace --file traces/trace2.txt --frames 64 --policy lru --json` prints, and a test
  holds three rows of the report against the files. No number in REPORT_M1.md changed; the words
  "stand-in" are gone and the section now says why each trace exists.
- `samples/`: a small database built with the tools themselves (`python -m samples.build`), 21
  plans, one per operator of Module 2 Section 2 including the handout's own hash join, 9 plans
  that must be rejected with one fault each, and the queries of Module 3 Section 5 as a file. A
  test runs every plan at B = 4 and B = 32 and checks the answers are the same bag.
- One line of assumption at the top of DESIGN_M1.md and DESIGN_M2.md and in DESIGN_M3.md section
  8: the traces are mine; `limit` is `{"op": "limit", "count": n}`; the set operators take
  `left` and `right`.

**AI mistake (waiting for something nobody had promised a date for).** The first versions of
the reports treated the missing files as a temporary gap and apologised for it in every row.
That made finished work look unfinished, and it put off the useful question, which was whether
my own traces were *good* traces. Answering it took one paragraph: what each of the five is for.

**What I learned.** A report measured on my own inputs is not a draft of the report measured on
someone else's. It needs its inputs to be on disk, reproducible and explained, and then it is
done.

**The same day, a second answer, covering everything.** No companion files at all: the test
inputs are left to me, the report measurements use my own data and workload, and each report
must document its assumptions, "including the schemas, sample data and workload you used". So:

- REPORT_M4.md lost its note that the supplied dataset and twenty queries were not available.
  It has a section 0 instead: the six schemas with row and page counts, how every column's
  values are made, the three correlated pairs, and the twenty queries, each with what it is
  there for. A script checks that the twenty in the report are the twenty the measurement runs,
  word for word. No measured number changed.
- REPORT_M1.md and REPORT_M2.md each open with the same three things in a short table or list.
  Most of it was already in the text, scattered; what was missing was how the rows are made.
- Writing the Module 2 list I found the report said a record is "213 bytes with the slot". It is
  4 + 4 + (2 + 200) = 210, and 214 with the slot. Both give 19 rows to a page, which is why no
  measurement ever disagreed with it. **AI mistake (wrong, and harmless until someone asks).**

## 2026-11-15: Integration, on a workload of my own

**Goal.** The last checkpoint was written around a dataset and twenty five queries to be
published three days before the demo. They will not be. So: my own dataset and queries, the
per-query report the handout asks for, and the PostgreSQL comparison made ready to run. Done
with AI assistance; **AI mistake** marks where it was wrong.

**Tried.** The data is the order database of REPORT_M4.md. Its generator was moved into one
function so that the same rows can be written as CSV files, and a check confirmed the function
still produces the stored database row for row (as a bag: first fit does not store rows in
arrival order, which made my first comparison say two tables had changed when nothing had).
`workload/load.sql` builds the database through the prompt, CREATE TABLE, LOAD, ANALYZE.
`workload/queries.sql` is twenty five new queries. `--report` gained a last column, the
optimizer's estimated cost, because a page count means little without the number it was
supposed to be.

**Writing queries that mean the same thing in PostgreSQL** was the part I had not thought about.
Four differences between my subset and PostgreSQL would each have produced a "wrong answer" that
was really a different question: PostgreSQL binds JOIN tighter than a comma, so a query mixing
them is parsed differently; it gives INTERSECT precedence over UNION, where the specification
here puts all set operators on one level; a LIMIT after an ORDER BY with ties may cut anywhere;
and SUM over nothing is NULL there and an error here. The workload avoids all four and
REPORT_INTEGRATION.md says so. String order is a fifth: PostgreSQL's depends on the database's
locale, so the comparison database is created with `--locale=C`.

**What broke.**

- **AI mistake (a crash waiting behind an aggregate).** Thinking about what PostgreSQL would
  return for SUM made me ask what mine does when a sum passes 2,147,483,647. Printed directly:
  fine, Python integers have no limit. But the same value as a join input, under a sort, a
  DISTINCT or a UNION is written to a temporary file with the record format, as a 4-byte INT,
  and `struct.pack` raised. Not an error message: a stack trace, in a system whose every tool
  promises one line. No test had a sum that large. A SUM column is now 8 bytes in temporary
  files and still an INT everywhere else; table pages are unchanged. Of four test queries over a
  sum of 4,000,000,000, the two that write it to a temporary file at B = 4, a join and a
  DISTINCT, crashed before and answer now; the other two, a sort and a UNION of two rows, never
  left memory and always worked, which is how the fault stayed hidden.
- **My test of the comparison tool was wrong before the tool was.** To imitate PostgreSQL's
  sixteen decimals I appended digits to a four-decimal average, which changed how it rounds.

**Measured.** At B = 32: sixteen of twenty five queries do exactly the I/O the optimizer
predicted, and none is off by more than 6 pages; median q-error at the root 1.59, worst 11.5.
The same answers at B = 8 and B = 4. The interesting row is W13: its root is off by a factor of
2, and inside the plan two filters are off by 10 and by 5, on the two pairs of columns that are
not independent.

**Checked.** All twenty five answers against the Bonus Project 1 engine at three budgets, in
the test suite. Against PostgreSQL: not yet. It is not installed on my laptop, and the handout
says the OpenStack instance; the files and the five commands are in REPORT_INTEGRATION.md
section 4, with two cells left to fill in.

**What I learned.** "Compare with PostgreSQL" is two tasks. One is running two systems. The
other, which comes first, is making sure the question is the same in both.

## 2026-11-15: a second opinion on the decisions

**What happened.** A friend who built the same project, in Java, went through my list of the
places where the documents disagree with each other and with the tools, and marked each against
his own implementation. Most of his "not implemented" lines are about his code. The useful part
was to take every point of substance and try it on mine.

**What held.** Two table names that would share a file on this file system are refused
(`Catalog`, `emp` after `Emp`), where his creates both and writing to one changes the other. The
`limit` node takes `count` and `n`. A column nobody needs, `Dept.title`, is pruned whichever
way the join order comes out. A sort runs in 3 frames and reproduces the specification's example.

**What broke.**

- **AI mistake (a fix that covered half of the fault).** Earlier (the Module 2 entry above)
  `exec run --json` was changed to fetch the first row before printing, so that a failing plan prints one error line
  and not half a JSON object. His note said: that cannot help a failure that comes later. He was
  right, and one plan in my own system does it: `union_all` opens its right input only after
  the left one is finished, so a right side that fails does so with rows already written. The
  output was `[3]error: ...`, not JSON. The first fix had been tested with the one failure I had
  in mind. Now the object is closed properly with an `"error"` member and the totals, the exit
  code is 1, and a test runs exactly that plan.

**Where we differ, and I kept mine.**

- *`exec trace`.* He prints each operator's own page counts, on the wording of section 1.4, and
  calls the 96 in the specification's example illustrative. I print what the example prints and
  keep the other behind `--own`. `counters()` returns own counts in both our systems, and the
  totals are the same; the difference is one default, and DESIGN_M2.md section 7 states mine.
- *ORDER BY on a column that is not selected.* His front end emits the canonical sort above the
  project and lets the optimizer move it. Mine carries the column through a wider project and
  drops it above the sort, so the logical plan is valid at every node without help. His shape
  is closer to the letter of the canonical order; mine needs nothing from Module 4.
- *The histogram.* For `age > 60` on the worked example his prints 2,000 with 20 buckets and
  mine 2,058. I have not seen his code; the likely reason is that a histogram which cuts every
  n / k rows, even inside a run of equal values, has a cut at row 8,000 of 10,000, which on this
  data is inside the run of 60s, and so counts exactly the top 2,000 rows. Mine never splits a
  run, so that "the bucket holding v" is one bucket. Both are equi-depth. DESIGN_M4.md section 2
  has the numbers.
- *Rule 4.* He pushes a select below every rename. I leave a rename that sits directly on a scan
  alone, because the specification's own "after" tree has the select above `rename e (scan Emp)`
  and the checklist asks for that tree to reproduce. Below a subquery or a union, mine pushes.

**Where he was right about my words.** Module 1 allows a scan one data page and one directory
page; it does not require two. ANALYZE *can* read a wide table once at a small B, by spooling
it; mine reads it once per batch of columns instead, which is fewer page operations and is what
DESIGN_M4.md says it does. And that the block nested loop needs no output frame follows from
handing each row straight to its parent, not from its read count matching a formula.

**What I learned.** A review of somebody else's code is still a list of questions to ask of
mine. One of them had an answer I did not like.

## 2026-11-15: the PostgreSQL comparison, run

**Goal.** Stop describing the comparison and do it. Done with AI assistance.

**Tried.** PostgreSQL is not installed on my laptop, but Docker is, with the official
`postgres:17` image already on it. A throwaway container, the `workload/` directory copied in,
a database created with locale C and encoding UTF8, then the two files as written:
`postgres_load.sql` and `postgres_queries.sql`. Its twenty five answer files copied back out
and compared with mine by `python -m workload.answers --compare`. The container was removed
afterwards.

**What broke.** Nothing. The two SQL files ran on the first try (they had only ever been tried
against SQLite, so that was not certain), PostgreSQL loaded the six row counts of the report,
returned the same number of rows as my system for every query, and the comparison printed
`25 of 25 the same`. PostgreSQL 17.12.

**What it does and does not show.** It shows that my system and PostgreSQL agree on these
twenty five queries over this data, including an average to four decimal places and the order
of every ORDER BY. It does not show that it was done where the handout says, on the OpenStack
instance: it was done on my laptop, and REPORT_INTEGRATION.md section 4 says so.
