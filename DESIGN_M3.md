# DESIGN_M3.md — SQL front end

SQL text in, logical plan out. Four steps, each a module under `sqlfront/`:

| Step | Module | In | Out |
|---|---|---|---|
| scan | `lexer.py` | text | tokens with line and column |
| parse | `parser.py` | tokens | syntax tree (`nodes.py`), names as written |
| analyse and translate | `translate.py` | syntax tree + catalog | logical plan, as JSON-shaped dicts |
| show | `explain.py` | either tree | indented text |

`sql.py` is the command line and the only file that prints. The grammar is in GRAMMAR_SQL.md.
Analysis and translation are one pass: a clause is resolved, checked and turned into its plan
node at the same moment, in the canonical order, so there is no second tree to keep in step.

## 1. Where duplicate elimination happens

**In one place.** `Translator.finish()` emits a `distinct` node, directly above the `project`,
when and only when the query wrote `DISTINCT`. No other line of the translator creates one.

Everything else that removes duplicates does so because of what the operator *is*, not because
the translator added something:

| Construct | Plan | Duplicates |
|---|---|---|
| a table | `scan` | kept. Identical rows stay identical rows. |
| select list | `project` | kept. Dropping columns can make rows equal; they stay. |
| `WHERE`, `HAVING` | `select` | kept |
| `,` and `JOIN ON` | `cross`, `join` | kept, and multiplied |
| `SELECT DISTINCT` | `distinct` above `project` | removed |
| `UNION ALL` | `union_all` alone | kept: m + n copies |
| `UNION` | `union` | removed by the operator |
| `INTERSECT`, `EXCEPT` | `intersect`, `except` | removed by the operator, even if both inputs had them |
| `GROUP BY` | `group_by` | one row per group, by construction |

**Why above the projection and not below.** Whether two rows are duplicates depends on which
columns they have. `SELECT DISTINCT city FROM Emp` must compare cities, not whole employees;
a distinct below the project would compare whole rows, find none equal, and remove nothing.

**Why a projection never brings a distinct with it.** In relational algebra on sets it had to:
`project[did]` over three employees in two departments is a set of two. In SQL the same query
returns three rows, and `SELECT did` and `SELECT DISTINCT did` are different queries with
different answers and different costs. Emitting distinct after every project would make the
first one wrong and slow.

**Why DISTINCT is not free.** `distinct` cannot emit a row until it knows whether it has seen it
before, so it must either remember every distinct row or sort the whole input. Every other
operator on the unary path (`select`, `project`, `limit`) handles one row and forgets it. That
is also the entire difference between `UNION` and `UNION ALL`: `union_all` passes two streams
through, `union` has to do the work of a distinct over both.

**DISTINCT above GROUP BY.** A `group_by` emits one row per distinct key, so if every grouping
column is in the select list the output rows already differ and `distinct` is left out:
`SELECT DISTINCT did, COUNT(*) FROM Emp GROUP BY did` has no distinct node. The condition
matters. `SELECT DISTINCT COUNT(*) FROM Emp GROUP BY did` projects the key away, two
departments with the same head count give two equal rows, and DISTINCT is the only thing that
removes one. There the node is emitted. The rule in the code is: skip it when the query is
aggregating and all grouping columns are in the select list (which includes the case of no
grouping columns at all, where there is one row).

**DISTINCT and ORDER BY.** `SELECT DISTINCT name FROM Emp ORDER BY salary` is rejected (schema
error). After distinct, one `name` stands for several rows with different salaries, so there is
no single salary to sort it by.

## 2. Scope and name resolution

**Building the scope.** The FROM clause is walked left to right. Each table reference appends
one entry `(key, columns)` to an ordered list:

- a table: key is the alias if there is one, otherwise the table name; columns come from the
  catalog. An unknown table is a name error. With an alias, the plan is `rename` over `scan`;
  without, the bare `scan`.
- a subquery: it is translated on its own, with its own fresh scope, and returns its plan and
  its output columns. Key is the alias; columns are the output columns *by their bare names*,
  so `(SELECT did, COUNT(*) AS n ...) AS t` offers `t.did` and `t.n` and nothing else. The plan
  is `rename t` over the subquery's plan. If two output columns have the same bare name
  (`SELECT e.name, m.name`) the rename would create `t.name` twice, so that is a schema error
  asking for an `AS`.

Before an entry is appended, its key is compared with the keys already there. A repeat is a
name error, `duplicate alias 'Emp' in FROM`. That is why `FROM Emp, Emp` fails and
`FROM Emp e, Emp m` works.

An alias replaces the table name. In `FROM Emp e` the name `Emp` is not in scope, and
`Emp.name` is an error that lists what FROM does have.

**Resolving a column**, `Translator.resolve()`, in this order:

1. Qualified `t.c`: find the entry with key `t`. None: name error, *unknown table or alias*.
   Found but no column `c`: name error, *unknown column 'c' in 't'*, listing its columns.
2. Unqualified `c`: collect every column named `c` over all entries. None: name error,
   *unknown column*. Two or more: ambiguity error naming each candidate, `e.did or d.did`.
   Exactly one: that column.

The result is always a `Col(table, name, type)` and is written into the plan as `table.name`.
No clause writes a name into the plan any other way, which is how "no unqualified name
survives" is kept: there is no code path that copies the query's spelling.

**The one exception** is names the query itself creates: the output name of an aggregate and a
select-list alias (`n`, `avg_sal`, `who`). They have no table, so they are bare, exactly as in
the specification's example `select "n > 5"`. A test walks every plan and checks that each bare
name is one that a `group_by` or `project` in that plan created.

**The scope of ON.** A join condition is resolved against the scope as it stands after its right
table was added: every table to its left, and its own right table. It cannot see a table that
comes later in FROM.

**SELECT \*** is every column of every entry, in FROM order then schema order, each written
qualified. Two tables sharing a column name is not an error here, since `Emp.did` and
`Dept.did` are different strings.

**ORDER BY** tries two places, in this order:

1. an unqualified name that is an output alias (an `AS` name or an aggregate's output name):
   the sort key is that name;
2. otherwise the FROM scope, by the rules above.

The sort sits above the project, so its key must exist in the project's output. If the resolved
column is in the select list, the key is its output name. If it is not (required case 9), the
project carries the column along, the sort uses it, and a second project above the sort drops
it again:

```
SELECT name FROM Emp ORDER BY salary DESC

project ["Emp.name"]
  sort [["Emp.salary","desc"]]
    project ["Emp.name","Emp.salary"]
      scan Emp
```

The canonical order project → sort is kept and the plan runs. The alternative, a sort that
names a column its input no longer has, would not run.

## 3. Aggregation

A block is **aggregating** if it has GROUP BY or an aggregate in its select list. Then:

- every bare column in the select list must be a grouping column, else an aggregate error;
- HAVING may use grouping columns and aggregates; any other column is an aggregate error;
- ORDER BY may use grouping columns and output names;
- `SELECT *` is an aggregate error.

An aggregate in WHERE or ON is an aggregate error in every query. Those clauses become nodes
below the `group_by`; at that point there are rows, not groups, and nothing to count yet.
The parser accepts the aggregate (GRAMMAR_SQL.md section 3) so that this can be reported as
what it is.

`SELECT COUNT(*) FROM Emp` has no GROUP BY, so it is a `group_by` with `keys: []`: the whole
table is one group, and there is one output row even when the table is empty.

**The aggregate naming rule.**

1. With `AS x`, the output name is `x`.
2. Without, it is the function, an underscore, and the argument with its dot turned into an
   underscore; `*` is written `star`: `count_star`, `avg_Emp_salary`, `min_e_name`.
3. An aggregate in HAVING that is the same function over the same column as one in the select
   list reuses that one's name and adds nothing. That is how `HAVING COUNT(*) > 5` becomes
   `n > 5` when the select list has `COUNT(*) AS n`. One that appears only in HAVING is added
   to the `group_by` under its rule-2 name and is not projected.
4. Two *different* aggregates with the same output name are a schema error.

The argument in the name is the resolved, qualified one, so the name does not depend on whether
the query wrote `salary` or `Emp.salary`.

## 4. Types

Two types, INT and VARCHAR; the length of a VARCHAR plays no part in comparison. Checked while
the condition is translated:

- the two sides of a comparison must have the same type, `=` and `<>` included. A column has
  its catalog type, a number is INT, a string is VARCHAR, an aggregate has its result type;
- `SUM` and `AVG` need an INT column; `COUNT` takes anything; `MIN` and `MAX` return their
  argument's type; `COUNT`, `SUM` and `AVG` return INT;
- the two sides of a set operator must have the same number of columns and the same type in
  each position. Names may differ; the result has the left side's names.

## 5. Conditions, and the link to Bonus Project 1

A resolved condition is rebuilt out of the Bonus Project 1 engine's own condition nodes
(`Compare`, `Logic`, `Attr`, `Literal`) and written to text by its `condition_text()`. The
string in the plan is therefore produced by the same module whose `parse_condition()` every
later module uses to read it, and a test parses every condition of every test plan back.

The structure of the condition is not touched: one `select` node holds the whole WHERE, AND is
not split, nothing is moved toward a leaf. Only names change (to qualified) and redundant
parentheses disappear, because they are not part of the tree.

One change to the Bonus engine was needed: its tokenizer now also reads `<>`.

## 6. Errors

`SqlError(category, message, position)` prints as
`error: <category>: <message> at line L, column C`. The position is that of the token the
message is about: the column reference for name and ambiguity errors, the comparison operator
for a type error, the aggregate for an aggregate error, the set operator for a schema error
between two blocks. The first error stops everything.

| Category | Raised by | Example |
|---|---|---|
| lexical | lexer | unterminated string, `!`, `1.5` |
| syntax | parser | expected FROM, found ... |
| name | translator | unknown table, unknown column, unknown alias, duplicate alias |
| ambiguity | translator | unqualified column found in two scope entries |
| type | translator | INT compared with VARCHAR, `SUM` of a VARCHAR |
| aggregate | translator | aggregate in WHERE, non-grouped column, `SUM(*)` |
| schema | translator | set operator with different column counts or types |

## 7. The catalog

The front end needs one thing from the catalog: given a table name, its columns and types, in
order. `sqlfront/catalog.py` is that interface (`get_table`, `create_table`, `list_tables`).
`load_catalog(DIR)` fills it from the storage engine's catalog pages when `DIR/catalog.db` exists,
and otherwise from `DIR/catalog.json`, a plain list of tables and columns that lets SQL be
compiled against a schema with no data behind it (the tests use it).

A `CREATE TABLE` in a file is checked and added to the in-memory catalog so that later
statements in the same file can use the table. Nothing is written to disk by this module.

## 8. Decisions the specification leaves open

**Assumption.** No plan schema or sample SQL was published, so where the specification shows no
JSON the field names are mine: `limit` is `{"op": "limit", "count": n, "child": …}`. The queries
of Section 5 are in `samples/section5.sql`.

These are choices, each made in one place.

| Question | Choice | Where |
|---|---|---|
| Field of `limit` | `"count"` | `finish()`, `plan_json.FIELDS` |
| `SELECT a AS x` (a plain column with an alias) | the project column is `["Emp.a", "x"]` instead of a string | `column_item()` |
| ORDER BY a column not selected | wide project, sort, narrow project (section 2) | `finish()` |
| DISTINCT above GROUP BY | omitted only when provably redundant (section 1) | `finish()` |
| `compile` of CREATE / INSERT | `{"op":"create_table",...}`, `{"op":"insert",...}`; not logical operators | `create_table()`, `insert()` |
| Several statements in one file | a JSON list; a single statement prints as one object | `sql.py` |
| Result type of AVG | INT, for type checking only | `grouping()` |
| Keywords as names | all keywords reserved | lexer |

## 9. What the tests cover

`tests/test_sql_lexer_parser.py`: the lexical rules, both precedence tables, error positions.
`tests/test_sql_cases.py`: each Section 5 example compared against the full expected plan, the
fifteen required cases by number, the bag rules, and the Section 9 checklist as assertions over
every test plan. `tests/test_sql_cli.py`: the three commands.

The plans are also *run*. The Bonus engine gained in-memory `group_by`, `sort` and `limit`, so a
test can compile SQL, load the plan through the JSON reader, execute it on five employees and
compare rows: UNION against UNION ALL, COUNT(*) over an empty table, the comma and JOIN forms
of the four-table chain giving the same rows.

## 10. Sources

Module 3 has no required measurements and no REPORT_M3.md among its deliverables, so the sources
for this checkpoint are listed here.

- Garcia-Molina, Ullman and Widom, *Database Systems: The Complete Book*, ch. 16.1 and 16.2.
  16.1 gives the preprocessor three checks after parsing: every relation exists, every attribute
  belongs to exactly one relation in scope and gets that relation attached to it, and the types
  agree. Section 2 is the second check taken literally. `resolve()` returns a column together
  with its table, and that pair is the only thing ever written into a plan, which is how no
  unqualified name survives. 16.2 has the law that duplicate elimination does nothing above a
  grouping, because a grouping's result has no duplicates. That is the DISTINCT over GROUP BY
  rule of section 1, with the condition the law does not need and SQL does: it holds for the
  output of the grouping, and a select list that drops a grouping column brings duplicates back.
- Ramakrishnan and Gehrke, *Database Management Systems*, ch. 5. Its conceptual evaluation of a
  query is the translation, one plan node per step: the product of the FROM list, the rows that
  pass the qualification, the columns of the select list, and duplicates removed only when
  DISTINCT is written. The last step is why a `project` never brings a `distinct` with it
  (section 1). The same chapter is the source for the asymmetry in the table of section 1: a
  plain query keeps duplicates by default and UNION removes them by default.
- The PostgreSQL documentation, *SELECT*. https://www.postgresql.org/docs/current/sql-select.html
  Three rules of section 2 are PostgreSQL's. An alias hides the table name completely, so
  `Emp.name` is an error after `FROM Emp e`. An ORDER BY name that is both an output name and an
  input column means the output name, which is the order `finish()` tries them in. And a query
  may order by a column it does not select, which is why required case 9 is given a plan that
  runs instead of an error.
- Robert Nystrom, *Crafting Interpreters*, the chapter on parsing expressions.
  https://craftinginterpreters.com/ Precedence is one method per level, lowest first:
  `condition` (OR), `and_condition`, `not_condition`, `comparison`. Each is a loop that builds a
  left-leaning tree, and no method compares precedences. GRAMMAR_SQL.md section 2 is the table
  those methods encode.
