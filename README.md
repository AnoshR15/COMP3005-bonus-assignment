# A database system in four modules

COMP 3005, Fall 2026. Project 2, built on the relational algebra engine of Bonus Project 1.

SQL goes in, a plan comes out, and rows are read from 4 KB pages on disk through a buffer pool.
Python 3.10 or newer, standard library only. Start with ARCHITECTURE.md.

| Checkpoint | Code | Tool | Documents |
|---|---|---|---|
| Engine refactor | `source/` | `ra.py` | GRAMMAR.md, REPORT.md |
| M1 storage engine | `store/` | `storage.py` | DESIGN_M1.md, REPORT_M1.md |
| M2 execution engine | `executor/` | `exec.py` | DESIGN_M2.md, REPORT_M2.md |
| M3 SQL front end | `sqlfront/` | `sql.py` | GRAMMAR_SQL.md, DESIGN_M3.md |
| M4 query optimizer | `optimizer/` | `opt.py` | DESIGN_M4.md, REPORT_M4.md |
| Integration | | `minidb.py` | ARCHITECTURE.md, REPORT_INTEGRATION.md |

DESIGN_LOG.md covers all of them. `python -m unittest` runs the tests (317). The sections below go
bottom up: the relational algebra engine first, then each module, then the whole system.

## The relational algebra engine (Bonus Project 1)

A relational algebra interpreter written from scratch in Python: a hand-written character
scanner, a recursive-descent parser, a parse-tree printer, the core operators with bag
semantics and schema checking, a data generator and a benchmark driver. No regular expressions,
parser generators, `eval`, or data-processing libraries anywhere. Only the standard library.

Since the start of Project 2 the engine is a library (see "Using it as a library" below): the
layers import independently, plans go to and from JSON, the operators pull tuples from whatever
source they are handed, and relations are bags instead of sets.

## Requirements

Python 3.10 or newer. Nothing to install. On Windows without a native Python the same commands
run under WSL with `python3`.

## Running

Print the parse tree of a query without executing it:

```
python ra.py --tree "project[Name](select[Age>30](Employees))"
```
```
Project(attrs=[Name])
└── Select(cond=Gt(Attr(Age), Num(30)))
    └── Relation(Employees)
```

Run a query against one or more relation files:

```
python ra.py --run data/example.ra "project[Name](select[Age>30](Employees))"
python ra.py --run data/generated/R.ra --run data/generated/S.ra "R join[R.b=S.b] S"
```

The result is a schema line followed by one tuple per line, sorted so output is reproducible.
After every run the tuple count and the operator counters (tuples examined by `select`, pairs
compared by `join`) are printed on standard error.

Print the plan as JSON without executing it:

```
python ra.py --json "select[R.a>500](R join[R.b=S.b] S)"
```

Tests, 317 in all (tests/required_cases.txt says which test covers each of the 25 required cases of
Bonus Project 1, and which two changed meaning under bag semantics):

```
python -m unittest -v                 # or: python -m unittest tests.test_parser
```

Performance study (see REPORT.md):

```
python -m source.generate_data --n 1000 --m 1000 --match-rate 0.1 --out data/generated
python -m source.benchmark all      # writes results/join.csv, unary.csv, match_rate.csv
python -m source.plot               # writes results/loglog.svg, prints the fitted slopes
```

## The language

A relation file:

```
// employees and their departments
Employees (EID, Name, Age, DID) = {
  E1, John, 32, D1
  E2, Alice, 28, D2
  E3, Bob, 29, D1
}
```

Queries:

```
select[ <condition> ]( <expr> )    project[ <attributes> ]( <expr> )    rename[ <NewName> ]( <expr> )
<expr> union <expr>     <expr> intersect <expr>     <expr> minus <expr>     <expr> times <expr>
<expr> join[ <condition> ] <expr>
distinct( <expr> )
```

Conditions combine comparisons (`=`, `!=`, `<`, `<=`, `>`, `>=`) with `and`, `or`, `not` and
parentheses. Operands are numbers, quoted strings, and attribute names, optionally qualified as
`Emp.DID`. The full grammar, the precedence table and every design decision are in GRAMMAR.md.

## Semantics

- Relations are bags. A tuple may occur more than once, in a relation file and in a result.
  Tuple equality is defined by `tuple_equal` in `source/operators.py`: same arity, numbers equal
  numerically, strings equal exactly.
- `select`: same schema as its input. The condition may compare two attributes.
- `project`: the listed attributes in the listed order. Duplicates are kept, so `project[DID]`
  over three employees gives three rows. Listing an attribute twice is an error.
- `distinct`: one copy of each tuple. This is the only place duplicates are removed, apart from
  the set operators below.
- `times`: all attributes of both inputs, qualified by relation name. A qualified name that would
  appear twice is an error.
- `join[c]`: `times` followed by `select[c]`, a theta join. It is evaluated one pair at a time
  without storing the product and gives exactly the same result.
- `union`, `intersect`, `minus`: schema of the left input. Both inputs must have the same number
  of attributes and the same type in each position (SQL's rule; names may differ). The result has
  no duplicates. The bag versions (SQL's `UNION ALL` and so on) are
  `setop(a, b, op, keep_all=True)`; they have no query syntax and appear in JSON as `union_all`.
- `group_by`, `sort` and `limit` exist as plan nodes and in-memory operators without query
  syntax; SQL is what produces them.
- `rename[N]`: the same attributes under the relation name `N`.
- Every attribute has a type, number or string, taken from the data. Comparing a number with a
  string is an error, not a silent false.

### Why the self join needs rename (case 20)

`Emp join[Emp.MgrID = Emp.EID] Emp` cannot be answered. The condition has to tell the employee
copy from the manager copy, but both operands are named `Emp`, so `Emp.EID` could mean either
one. The product would also contain every attribute twice under the same qualified name
(`Emp.EID`, `Emp.EID`, ...), which the engine rejects as a schema error. `rename[E2](Emp)` gives
one copy a different name, so `rename[E2](Emp) join[Emp.MgrID = E2.EID] Emp` has an unambiguous
condition and a schema in which `E2.EID` and `Emp.EID` are distinct columns.

## Storage engine and execution engine (Project 2, Modules 1 and 2)

`store/` is the storage layer: one file per table of 4 KB slotted pages, heap files with
directory pages, a buffer pool with LRU and CLOCK, and a catalog in `catalog.db`. `executor/`
runs a physical plan against it inside a budget of B frames. Designs in DESIGN_M1.md and
DESIGN_M2.md, measurements in REPORT_M1.md and REPORT_M2.md.

```
python storage.py create-table --db db --name R --schema "a:INT,b:VARCHAR(20),c:INT"
python storage.py load   --db db --table R --file r.csv
python storage.py scan   --db db --table R --frames 8 --limit 20 --json
python storage.py layout --db db --table R --page 1
python storage.py trace  --db db --file trace.txt --frames 16 --policy lru --json

python exec.py run   --db db --plan plan.json --frames 32 --json
python exec.py trace --db db --plan plan.json --frames 32 [--verbose] [--own]

python -m bench.m1_measure          # the measurements of REPORT_M1.md, into results/m1/
python -m bench.m2_measure          # the measurements of REPORT_M2.md, into results/m2/
```

No trace files, plan files or datasets were published with the specifications, so the ones here
are my own:

```
traces/trace1.txt ... trace5.txt     the five traces of REPORT_M1.md; example.txt is Section 5.3
python -m samples.build              a small database in ./db, built with storage and opt analyze
samples/plans/*.json                 one physical plan per operator, on that database
samples/plans/bad/*.json             plans that must be rejected, one fault each
samples/section5.sql                 the queries of Module 3, Section 5

python storage.py trace --file traces/trace3.txt --frames 64 --policy lru --json
python exec.py run   --plan samples/plans/hash_join.json --frames 32 --json
python exec.py trace --plan samples/plans/sort_20xB.json --frames 4
python sql.py compile --file samples/section5.sql --json
python minidb.py --file samples/section5.sql
```

| Path | Contents |
|---|---|
| `storage.py`, `exec.py` | command lines |
| `store/disk.py` | disk manager: the only code that touches a file; counts every page read and write |
| `store/page.py`, `store/record.py` | slotted page and record formats |
| `store/buffer.py` | buffer pool: pinning, dirty pages, LRU and CLOCK, reserved frames |
| `store/heap.py`, `store/catalog.py`, `store/db.py` | heap files with directory pages and first fit, the catalog, the CSV loader |
| `executor/operators.py` | iterator interface, the frame-division rule, scan, filter, project, rename, limit, union_all |
| `executor/sort.py`, `executor/groupby.py`, `executor/joins.py`, `executor/setops.py` | external sort; group_by and distinct; the three joins; union, intersect, except |
| `executor/build.py`, `executor/run.py`, `executor/context.py` | plan validation; running and tracing; counters, workspaces, temporary files |
| `bench/m1_measure.py`, `bench/m2_measure.py` | the measurement studies |
| `tests/test_store.py`, `tests/test_exec.py`, `tests/test_exec_fuzz.py` | storage; every operator, counters and the budget; random plans against the reference engine |

## The whole system (Project 2, integration)

`minidb.py` is one prompt over all four modules: SQL in, rows out. ARCHITECTURE.md describes the
system and the interface between each pair of modules.

```
python minidb.py --db db                                  # a prompt; statements end with ;
python minidb.py --db db --file script.sql
python minidb.py --db db --file queries.sql --report      # one summary line per query
```

```
minidb> CREATE TABLE Emp (eid INT, name VARCHAR(20), age INT, did INT);
minidb> LOAD Emp FROM 'emp.csv';
minidb> ANALYZE Emp;
minidb> SET frames = 16;
minidb> SELECT did, COUNT(*) AS n FROM Emp WHERE age > 30 GROUP BY did ORDER BY n DESC LIMIT 5;
minidb> EXPLAIN SELECT ...;            -- the physical plan with estimated rows and cost
minidb> EXPLAIN ANALYZE SELECT ...;    -- run it: estimated against actual, line by line
```

The integration workload, my own since none was published (REPORT_INTEGRATION.md):

```
python -m workload.build                                         # the order database as six CSV files
python minidb.py --db wdb --file workload/load.sql               # create, load, analyze, through the prompt
python minidb.py --db wdb --file workload/queries.sql --report   # 25 queries: rows, I/O, time, est. vs actual
python -m workload.answers --db wdb                              # the answers as CSV files
python -m workload.answers --compare workload/answers workload/postgres   # against PostgreSQL's
```

The four module tools (`storage.py`, `exec.py`, `sql.py`, `opt.py`) work on the same database
directory. Every tool names it with `--db DIR` or `--catalog DIR` (one option, two spellings) and
uses the directory `db` when neither is given, so the command lines of the handout run as
printed; `tests/test_handout_cli.py` runs each of them that way.

## Query optimizer (Project 2, Module 4)

`optimizer/` turns a logical plan into a physical one: ANALYZE with equi-depth histograms,
selectivity estimation, seven rewrite rules, a page I/O cost model, and dynamic programming over
left-deep join orders. Design in DESIGN_M4.md, measurements in REPORT_M4.md.

```
python opt.py analyze  --db db --table R --buckets 20        # or --all
python opt.py plan     --db db --logical q.json --frames 32 --json
python opt.py explain  --db db --logical q.json --frames 32  # --rewrite: the logical plan before and after;
                                                             # --subtree, --own: other meanings of est_cost
python opt.py dp-table --db db --logical q.json --frames 32

python opt.py explain  --db db --frames 32 --sql "SELECT e.name FROM Emp e, Dept d WHERE e.did = d.did"
python -m bench.m4_measure          # the measurements of REPORT_M4.md, into results/m4/
```

The whole path, by hand:

```
python sql.py compile --db db --query "SELECT ..." --json > logical.json
python opt.py plan    --db db --logical logical.json --frames 32 --json > physical.json
python exec.py run    --db db --plan physical.json --frames 32 --json
```

| Path | Contents |
|---|---|
| `opt.py` | command line |
| `optimizer/stats.py` | ANALYZE: one scan, one external sort per column, statistics into the catalog |
| `optimizer/estimate.py` | selectivity and cardinality |
| `optimizer/rewrite.py` | the seven rewrite rules |
| `optimizer/cost.py`, `optimizer/planner.py` | cost formulas; physical choices, join enumeration, explain, the DP table |
| `bench/m4_measure.py` | the four measurement studies, on my own order database and twenty queries (REPORT_M4.md section 0) |
| `tests/test_opt.py`, `tests/optbase.py` | the checklist, statistics, selectivity, each rule, enumeration, the command line |

## SQL front end (Project 2, Module 3)

`sqlfront/` turns SQL into a logical plan in JSON. Grammar in GRAMMAR_SQL.md, design in DESIGN_M3.md.

```
python sql.py parse   --query "SELECT e.name FROM Emp e WHERE e.age > 30" --explain
python sql.py compile --db tests/data/sqldb --file q.sql --json
python sql.py compile --db tests/data/sqldb --file q.sql --explain
python sql.py check   --db tests/data/sqldb --file q.sql
```

`--db DIR` is a database directory of the storage engine, or a directory holding only a
`catalog.json` (table names, column names and types). An error is one line,
`error: <category>: <message> at line L, column C`, with exit code 1.

| Path | Contents |
|---|---|
| `sql.py` | command line |
| `sqlfront/lexer.py` | SQL scanner with line and column positions |
| `sqlfront/parser.py`, `sqlfront/nodes.py` | recursive-descent parser and its syntax tree |
| `sqlfront/translate.py` | name resolution, aggregate and type rules, translation to a logical plan |
| `sqlfront/catalog.py` | the catalog interface and its JSON stand-in |
| `sqlfront/explain.py` | indented text for a syntax tree and for a plan |
| `tests/test_sql_*.py`, `tests/sqlbase.py` | lexer, parser, the 15 required cases, the command line |

## Using it as a library

Nothing under `source/` prints, reads a file or looks at the command line, except the loader
(`relation_file.py`), which exists to read files, and the benchmark, generator and plot scripts.
`tests/test_layers.py` checks this, and checks that each of the four layers imports alone.

| Layer | Module | Imports from the package |
|---|---|---|
| tokenizer | `source/tokenizer.py` | `errors` |
| parser | `source/parser.py` | `ast`, `errors` (it takes a token list, not text) |
| tree | `source/ast.py`, `source/tree.py` | nothing else |
| operators | `source/operators.py` | `ast`, `errors` |

**Conditions on their own** (`source/condition.py`):

```python
from source.condition import parse_condition, bind, compile_condition, condition_text
from source.operators import SchemaAttr, NUMBER

schema = [SchemaAttr('R', 'a', NUMBER), SchemaAttr('R', 'b', NUMBER), SchemaAttr('S', 'b', NUMBER)]
cond = parse_condition('R.b = S.b and R.a > 500')      # string -> tree
test = bind(cond, schema)                              # names and types checked here, once
test((501, 7, 7))                                      # True
condition_text(cond)                                   # 'R.b = S.b and R.a > 500'
```

**Plans as JSON** (`source/plan_json.py`): `to_json(tree)`, `from_json(dict)`, `dumps(tree)`,
`loads(text)`. The format is listed at the top of that file. A plan that does not follow it is
rejected with a `PlanError` naming the node, for example
`Node 'select': missing required field 'condition'`.

**Data sources** (`source/engine.py`): `evaluate(tree, source, counters)` asks `source.scan(name)`
for each leaf and gets back a `Relation`, which is a schema and any iterable of rows.
`MemorySource(relations)` serves relations loaded from files. The operators pull rows one at a
time and the result is itself lazy; call `.materialize()` to collect it. Two kinds of operator
still hold rows in memory: `join` and `times` copy their right input, and `distinct` and the set
operators remember rows they have seen. Module 2 replaces those with versions that respect a
page budget.

## Errors

Every error is one line beginning with `Error:`. The program never prints a stack trace. Lexical
and syntax errors carry a position in the query.

| Category | Example query | Message |
|---|---|---|
| Lexical | `select[Name='Bob](R)` | Unterminated string at position 12 |
| Syntax | `select[Age>30](R` | Expected ')' at end of query (position 16) |
| Name | `select[Salary>1](Emp)` | Unknown attribute 'Salary'; the schema is Emp.EID, Emp.Name, ... |
| Schema | `Emp union Dept` | Emp union Dept: not union compatible (Emp has 5 attributes but Dept has 2) |
| Type | `select[Age>'30'](Emp)` | Cannot compare number to string in 'Age > '30'' |
| Data file | a column holding `1` and `x` | Attribute 'a' of M mixes numbers and strings |

Names and types are checked against the schema before any tuple is examined, so they are reported
even when the input is empty and when the comparison sits on the unreached side of an `and`.

## Layout

| Path | Contents |
|---|---|
| `ra.py` | command line |
| `source/tokenizer.py` | character scanner with token positions and maximal munch |
| `source/parser.py` | recursive-descent parser, one method per grammar rule |
| `source/ast.py` | tree node types |
| `source/tree.py` | parse tree printer |
| `source/operators.py` | the operators, schema checks, condition binding, tuple equality, counters |
| `source/condition.py` | the condition language on its own: parse, bind, write back as text |
| `source/plan_json.py` | the plan tree to and from JSON |
| `source/engine.py` | bottom-up evaluation of a tree against a data source |
| `source/relation_file.py` | loader for relation definition files |
| `source/generate_data.py` | data generator for the performance study |
| `source/benchmark.py`, `source/plot.py` | experiment driver and the log-log chart |
| `tests/base.py` | test base class: loads the relation files a test class lists, wraps parse and evaluate |
| `tests/test_tokenizer.py`, `tests/test_parser.py`, `tests/test_operators.py` | the 25 cases of section 7 (numbered in each docstring), the five error categories, the fixed defects |
| `tests/test_relation_file.py`, `tests/test_generate_data.py`, `tests/test_cli.py` | loader, generator, command line |
| `tests/test_condition.py`, `tests/test_plan_json.py`, `tests/test_layers.py`, `tests/test_bags.py` | the library refactor: standalone conditions, JSON round trip, layer independence and data sources, bag semantics and qualified names |
| `tests/data/` | small relation files the tests load; `bad/` holds one malformed file per data error |
| `results/` | CSV files written by the benchmark, the chart, and the run log |
| `GRAMMAR.md`, `REPORT.md`, `DESIGN_LOG.md` | the written deliverables |

## Known limitations of the relational algebra engine (`ra.py`)

These are about the Bonus Project 1 engine and its relation files, not the database system
above it, whose limits are listed in each DESIGN document and in ARCHITECTURE.md section 5.

- No nulls. A relation must contain at least one tuple because attribute types are taken from
  the data, so an empty relation definition is rejected.
- Only the ASCII spellings of the operators are accepted, not the Unicode symbols.
- Numbers are integers or decimals such as `-3` and `2.5`. There is no exponent notation.
- A relation cannot be named `select`, `project`, `rename` or `distinct`, and an attribute named `not` cannot
  start a comparison (GRAMMAR.md section 6). Every other keyword works as a name.
- Its joins are nested loops with no indexes. The measured cost, up to 64000 x 64000 tuples, is
  in REPORT.md. (The database system has three join algorithms; see DESIGN_M2.md.)
- `ra.py` reads its relations into memory from relation files. Reading pages from disk is the
  storage engine's job, in `store/`.
- Output is sorted by value for reproducibility. Relations are bags and have no order of their own.
- REPORT.md was measured on the Bonus Project 1 engine, with sets. The comparison counts are
  unchanged; `project` no longer removes duplicates, so its timings there describe the old code.
