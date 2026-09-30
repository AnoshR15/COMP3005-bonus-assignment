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
