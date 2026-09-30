# Relational Algebra Engine

COMP 3005 Bonus Project 1, Fall 2026.

A relational algebra interpreter written from scratch in Python: a hand-written character
scanner, a recursive-descent parser, a parse-tree printer, the six core operators with set
semantics and schema checking, a data generator and a benchmark driver. No regular expressions,
parser generators, `eval`, or data-processing libraries anywhere. Only the standard library.

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

Tests, one module per source module, 48 tests in all (tests/required_cases.txt says which test covers each of the 25 required cases):

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
```

Conditions combine comparisons (`=`, `!=`, `<`, `<=`, `>`, `>=`) with `and`, `or`, `not` and
parentheses. Operands are numbers, quoted strings, and attribute names, optionally qualified as
`Emp.DID`. The full grammar, the precedence table and every design decision are in GRAMMAR.md.

## Semantics

- Relations are sets. Duplicates collapse on input and after every operator. Tuple equality is
  defined by `tuple_equal` in `source/operators.py`: same arity, numbers equal numerically,
  strings equal exactly.
- `select`: same schema as its input. The condition may compare two attributes.
- `project`: the listed attributes in the listed order, duplicates removed afterwards. Listing an
  attribute twice is an error.
- `times`: all attributes of both inputs, qualified by relation name. A qualified name that would
  appear twice is an error.
- `join[c]`: `times` followed by `select[c]`, a theta join. It is evaluated one pair at a time
  without storing the product and gives exactly the same result.
- `union`, `intersect`, `minus`: schema of the left input. Both inputs must have the same number
  of attributes, with the same names in the same order and the same types.
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
| `source/operators.py` | the six operators, schema checks, condition binding, tuple equality, counters |
| `source/engine.py` | bottom-up evaluation of a tree |
| `source/relation_file.py` | loader for relation definition files |
| `source/generate_data.py` | data generator for the performance study |
| `source/benchmark.py`, `source/plot.py` | experiment driver and the log-log chart |
| `tests/base.py` | test base class: loads the relation files a test class lists, wraps parse and evaluate |
| `tests/test_tokenizer.py`, `tests/test_parser.py`, `tests/test_operators.py` | the 25 cases of section 7 (numbered in each docstring), the five error categories, the fixed defects |
| `tests/test_relation_file.py`, `tests/test_generate_data.py`, `tests/test_cli.py` | loader, generator, command line |
| `tests/data/` | small relation files the tests load; `bad/` holds one malformed file per data error |
| `results/` | CSV files written by the benchmark, the chart, and the run log |
| `GRAMMAR.md`, `REPORT.md`, `DESIGN_LOG.md` | the written deliverables |

## Known limitations

- No nulls. A relation must contain at least one tuple because attribute types are taken from
  the data, so an empty relation definition is rejected.
- Only the ASCII spellings of the operators are accepted, not the Unicode symbols.
- Numbers are integers or decimals such as `-3` and `2.5`. There is no exponent notation.
- A relation cannot be named `select`, `project` or `rename`, and an attribute named `not` cannot
  start a comparison (GRAMMAR.md section 6). Every other keyword works as a name.
- Joins are nested loops with no indexes. The measured cost, up to 64000 x 64000 tuples, is in
  REPORT.md.
- Everything is held in memory. There is no storage layer yet; that is a later project.
- Output is sorted by value for reproducibility. Relations are sets and have no order of their own.
