# REPORT_INTEGRATION.md — The whole system on one workload

One database, twenty five queries, every query through all four modules: SQL text into the
prompt, rows out, read from pages on disk. For each query this reports the result row count, the
page I/O, the wall time, and the rows estimated at the root against the rows that came out.

## 0. Assumptions: the schemas, the data and the workload

No dataset and no queries were published for this checkpoint, so both are mine. The data is the
order database of REPORT_M4.md, row for row; the twenty five queries are new, written for this
report.

### Schemas

`workload/load.sql` creates them through the prompt:

| Table | Columns | Rows | Data pages |
|---|---|---|---|
| Customer | cid INT, name VARCHAR(16), city VARCHAR(12), country VARCHAR(10), segment INT, note VARCHAR(60) | 300 | 7 |
| Orders | oid INT, cid INT, odate INT, shipdate INT, status INT, total INT, note VARCHAR(60) | 2,000 | 40 |
| Item | oid INT, lineno INT, pid INT, qty INT, price INT, note VARCHAR(40) | 3,000 | 42 |
| Product | pid INT, pname VARCHAR(20), category INT, brand INT, weight INT, note VARCHAR(60) | 200 | 5 |
| Supplier | sid INT, sname VARCHAR(16), country VARCHAR(10) | 30 | 1 |
| Supply | pid INT, sid INT, cost INT | 500 | 2 |

### Sample data

Six CSV files in `workload/data/`, no header line, written by `python -m workload.build` from one
fixed seed. They are what `LOAD` reads here and what `\copy` reads in PostgreSQL, so both systems
hold the same rows. How each column's values are made is in REPORT_M4.md section 0; what matters
for reading this report:

- `Customer.city` determines `Customer.country` (30 cities, 6 countries), and `Orders.shipdate`
  is `Orders.odate` plus 1 to 5: two pairs of columns that are not independent.
- `Orders.status` is 0 for 60 percent of the orders; `Item.pid` is skewed, a few products hold
  most of the items.
- The `note` columns are padding, there to give the tables pages. No query selects one, so
  every join input is a few pages wide after its projection.

### Workload

`workload/queries.sql`. Every query is also valid PostgreSQL with the same meaning, which rules
a few things out: no query mixes commas with JOIN (PostgreSQL binds JOIN tighter), none mixes
INTERSECT with UNION or EXCEPT without parentheses (PostgreSQL gives INTERSECT precedence, this
subset does not), every LIMIT comes with an ORDER BY on a key that has no ties, and no aggregate
but COUNT is taken over an input that can be empty (PostgreSQL answers NULL; there is no NULL
here).

| | Query | What it is there for |
|---|---|---|
| W01 | `SELECT cid, name, city FROM Customer WHERE country = 'Japan' AND segment <> 0` | a string equality and a `<>` |
| W02 | `SELECT oid, total FROM Orders WHERE total >= 4900 OR total <= 50` | OR |
| W03 | `SELECT oid FROM Orders WHERE NOT (status = 0 OR odate < 900)` | NOT over a parenthesised OR |
| W04 | `SELECT DISTINCT country, city FROM Customer ORDER BY country, city` | DISTINCT with ORDER BY |
| W05 | `SELECT pid, pname, weight FROM Product WHERE category = 2 ORDER BY weight DESC, pid LIMIT 5` | two sort keys, one descending, and LIMIT |
| W06 | `SELECT name FROM Customer ORDER BY cid DESC LIMIT 7` | ORDER BY a column that is not selected |
| W07 | `SELECT COUNT(*) FROM Item WHERE qty = 11` | COUNT(*) over no rows: one row, 0 |
| W08 | `SELECT MIN(total), MAX(total), SUM(total), AVG(total), COUNT(*) FROM Orders WHERE status = 2` | all five aggregates, no GROUP BY |
| W09 | `SELECT status, COUNT(*) AS n, MIN(odate), MAX(shipdate) FROM Orders GROUP BY status` | GROUP BY one column |
| W10 | `SELECT country, segment, COUNT(*) AS n FROM Customer GROUP BY country, segment HAVING COUNT(*) >= 12` | GROUP BY two columns, HAVING |
| W11 | `SELECT c.country, SUM(o.total) AS revenue, AVG(o.total) AS average FROM Customer c JOIN Orders o ON c.cid = o.cid WHERE o.status <> 0 GROUP BY c.country ORDER BY revenue DESC` | grouping over a join, ordered by an aggregate |
| W12 | `SELECT c.name, o.oid, o.total FROM Customer c JOIN Orders o ON c.cid = o.cid WHERE c.city = 'Oslo' AND o.total > 4000` | two tables, a filter on each |
| W13 | `SELECT c.name, o.oid FROM Customer c, Orders o WHERE c.cid = o.cid AND o.odate > 900 AND o.shipdate > 900 AND c.city = 'Arica' AND c.country = 'Chile'` | a comma join, with both dependent pairs in the WHERE |
| W14 | `SELECT o.oid, i.lineno, p.pname FROM Orders o JOIN Item i ON o.oid = i.oid JOIN Product p ON i.pid = p.pid WHERE o.status = 4 AND p.category = 0 AND i.qty >= 9` | three tables |
| W15 | `SELECT c.name, p.pname, i.qty FROM Customer c JOIN Orders o ON c.cid = o.cid JOIN Item i ON o.oid = i.oid JOIN Product p ON i.pid = p.pid WHERE c.segment = 4 AND p.brand = 3` | four tables |
| W16 | `SELECT s.sname, p.pname, i.price FROM Supplier s JOIN Supply y ON s.sid = y.sid JOIN Product p ON y.pid = p.pid JOIN Item i ON i.pid = p.pid JOIN Orders o ON o.oid = i.oid WHERE s.country = 'Kenya' AND y.cost > 90 AND o.status = 3` | five tables |
| W17 | `SELECT c.name, s.sname FROM Customer c JOIN Orders o ON c.cid = o.cid JOIN Item i ON o.oid = i.oid JOIN Product p ON i.pid = p.pid JOIN Supply y ON y.pid = p.pid JOIN Supplier s ON s.sid = y.sid WHERE c.city = 'Tokyo' AND s.country = 'Peru' AND i.qty = 1` | six tables |
| W18 | `SELECT a.cid, b.cid FROM Customer a JOIN Customer b ON a.city = b.city WHERE a.cid < b.cid AND a.segment = 0 AND b.segment = 0` | a self join, with an inequality between the copies |
| W19 | `SELECT p.pid, y.sid FROM Product p JOIN Supply y ON p.pid = y.pid AND y.cost > p.weight WHERE p.brand < 2` | an equality and an inequality in one join condition |
| W20 | `SELECT a.sid, b.sid FROM Supplier a JOIN Supplier b ON a.sid < b.sid WHERE a.sid > 25` | a join on an inequality alone |
| W21 | `SELECT c.name, t.orders, t.spent FROM (SELECT cid, COUNT(*) AS orders, SUM(total) AS spent FROM Orders GROUP BY cid) AS t JOIN Customer c ON t.cid = c.cid WHERE t.orders >= 12` | a subquery in FROM, joined to a table |
| W22 | `SELECT country FROM Customer UNION ALL SELECT country FROM Supplier` | UNION ALL |
| W23 | `SELECT country FROM Customer UNION SELECT country FROM Supplier` | UNION on the same inputs |
| W24 | `SELECT cid FROM Orders WHERE total > 4950 INTERSECT SELECT cid FROM Orders WHERE status = 4` | INTERSECT |
| W25 | `(SELECT pid FROM Product WHERE category = 1 UNION SELECT pid FROM Supply WHERE cost < 3) EXCEPT SELECT pid FROM Item WHERE qty = 10` | EXCEPT, with parentheses around a UNION |

## 1. How it was run

```
python -m workload.build                                            the six CSV files
python minidb.py --db wdb --file workload/load.sql                  CREATE TABLE, LOAD, ANALYZE
python minidb.py --db wdb --file workload/queries.sql --report      the table of section 2
```

B = 32, the prompt's default. Each query gets a buffer pool of its own, so its page counts do
not depend on the queries before it, and they are the same on every run. Wall time is not: it
was measured once, on a laptop, with Python under WSL reading a Windows file system.

## 2. Results

| | result rows | page reads | page writes | wall time, ms | estimated rows at the root | actual rows | q-error | estimated cost |
|---|---|---|---|---|---|---|---|---|
| W01 | 39 | 7 | 0 | 18 | 37 | 39 | 1.05 | 7 |
| W02 | 46 | 40 | 0 | 21 | 50 | 46 | 1.09 | 40 |
| W03 | 72 | 40 | 0 | 19 | 76 | 72 | 1.06 | 40 |
| W04 | 30 | 7 | 0 | 17 | 180 | 30 | 6.00 | 8 |
| W05 | 5 | 5 | 0 | 17 | 5 | 5 | 1.00 | 5 |
| W06 | 7 | 7 | 0 | 20 | 7 | 7 | 1.00 | 7 |
| W07 | 1 | 42 | 0 | 31 | 1 | 1 | 1.00 | 42 |
| W08 | 1 | 40 | 0 | 35 | 1 | 1 | 1.00 | 40 |
| W09 | 5 | 40 | 0 | 23 | 5 | 5 | 1.00 | 40 |
| W10 | 11 | 7 | 0 | 33 | 11 | 11 | 1.00 | 7 |
| W11 | 6 | 49 | 2 | 152 | 6 | 6 | 1.00 | 57 |
| W12 | 3 | 48 | 1 | 69 | 11 | 3 | 3.67 | 49 |
| W13 | 6 | 48 | 1 | 107 | 3 | 6 | 2.00 | 49 |
| W14 | 29 | 89 | 2 | 226 | 46 | 29 | 1.59 | 92 |
| W15 | 52 | 103 | 8 | 416 | 35 | 52 | 1.49 | 112 |
| W16 | 2 | 99 | 4 | 562 | 23 | 2 | 11.50 | 104 |
| W17 | 1 | 104 | 7 | 529 | 4 | 1 | 4.00 | 113 |
| W18 | 52 | 15 | 1 | 92 | 12 | 52 | 4.33 | 16 |
| W19 | 2 | 7 | 0 | 36 | 6 | 2 | 3.00 | 7 |
| W20 | 6 | 2 | 1 | 70 | 12 | 6 | 2.00 | 4 |
| W21 | 14 | 48 | 1 | 112 | 101 | 14 | 7.21 | 49 |
| W22 | 330 | 8 | 0 | 47 | 330 | 330 | 1.00 | 8 |
| W23 | 6 | 8 | 0 | 34 | 12 | 6 | 2.00 | 8 |
| W24 | 8 | 82 | 2 | 169 | 14 | 8 | 1.75 | 80 |
| W25 | 24 | 51 | 2 | 135 | 50 | 24 | 2.08 | 50 |

The last column is not asked for. It is the optimizer's estimate of reads plus writes, and it is
what the page counts have to be held against.

Median q-error at the root 1.59; seventeen of twenty five within 2; worst 11.50.

## 3. What the numbers say

### Page I/O

**A query on one table costs that table's pages and nothing else**: 7 for Customer, 40 for
Orders, 42 for Item, 5 for Product. A filter, a projection and a limit read nothing of their
own, and at B = 32 a sort, a distinct or a grouping of a few hundred rows fits in memory. W07
reads all 42 pages of Item to return a single 0.

**Every join in the workload is a nested loop, and that is the right choice here.** No query
selects a `note` column, so the projections the optimizer pushes down make every join input
small: Orders shrinks from 40 pages to 6, Item from 42 to about 12. An outer input that fits in
B − 2 frames makes the cost of a block nested loop the two inputs read once, which nothing beats. W12 is the pattern: 7 pages of Customer and 40 of
Orders are read, the filtered inner side is written to a one-page temporary file and read back
once: 48 reads and 1 write, against 49 predicted.

**The six-table join, W17, costs 104 reads and 7 writes.** The six tables are 97 pages together
(40 + 42 + 5 + 7 + 2 + 1). Every page is read once; the other 7 reads and the 7 writes are the
inner inputs that were projected or filtered and so were written down before being joined.

**Predicted against done.** Sixteen of the twenty five are exact, and none is off by more than
6 pages. Where they differ:

- *W11, 57 predicted, 51 done; W04, 8 and 7.* W11 is a sort over a grouping over a join. The
  cost model charges the grouping and the sort for reading their inputs; the executor hands the
  rows straight from one operator to the next and reads nothing (DESIGN_M4.md section 4, the
  known over-charge). W04 is the same thing on one page: a sort over a distinct.
- *W24, 80 predicted, 84 done; W25, 50 and 53.* INTERSECT and EXCEPT sort both inputs and walk
  them in step, and each sorted input is written as a run and read back even when it is one
  page. Those pages the model does not count.
- *W20, 4 predicted, 3 done.* Supplier is one page and the query joins it to itself. The second
  scan found the page still in the buffer pool.
- *W14 to W17, off by 1 or 2.* These agree for a reason that is not flattering: two errors of
  opposite sign. `EXPLAIN ANALYZE` of W16 shows its lowest join predicted at 84 and done in 89,
  because joins that run at the same time share the frames and its block was smaller than the
  model assumed. Each join above it is then charged for reading the result of the one below,
  which the executor pipelines. By the root the plan is predicted at 104 and done in 103.

### Estimated against actual rows

The filters on one column are estimated almost exactly: 37 for 39, 50 for 46, 76 for 72. Under a
LIMIT the root is right by construction. The eight roots that are off by more than 2 have five
causes between them.

- **Columns that are not independent (W04, 6.0; W13).** `SELECT DISTINCT country, city` is
  estimated at 180 rows, 6 countries times 30 cities, and there are 30, because a city is in one
  country. W13 hides the same thing: its root says 3 against 6, a q-error of 2, but inside the
  plan the filter on the two dates is estimated at 17 rows and passes 178, and the filter on
  city and country at 3 and passes 14. Two errors of 10 and 5 meet in a join and show as 2 at
  the root. The root is not the place to look for them.
- **A default where there are no statistics (W21, 7.2; W18, 4.3; W19, 3.0).** W21 filters on
  `COUNT(*)`, a column that exists only inside the query: a third of the 300 groups are assumed
  to pass, and 14 do. W18 and W19 each compare two columns with `<` or `>`, which gets the
  documented default of one tenth. In W18 the truth is about a third, 52 of the 163 pairs the
  join produces: leave out a customer paired with itself, and half of the rest have the smaller
  id first. In W19 it is far less than a tenth, because a cost is at most 99 and a weight goes
  up to 299.
- **Errors that grow up a chain of joins (W16, 11.5; W17, 4.0).** In W16 the first join is
  estimated at 420 rows and produces 310, a factor of 1.35. The next is 137 against 57, a factor
  of 2.4, and the root 23 against 2. No single step is badly wrong; each is multiplied onto the
  last.
- **Small numbers (W12, 3.7).** Both filters are right, 398 for 399 and 8 for 7, and the join
  formula gives 11. Seven customers in Oslo have about 47 orders, a fifth of which should be
  large: 9 expected, 3 found. That is chance, not a wrong assumption.
- **Set operators (W25, 2.1; and W23, exactly 2).** EXCEPT is estimated as if the right side
  removed nothing: 50 rows, and 24 are left. UNION adds the distinct counts of its two inputs as
  if they shared no value: 6 countries and 6 countries make 12, and they are the same six.

Whether any of these led to a worse plan, this workload cannot show: its inputs are so small
that predicted and actual I/O agree to within 6 pages for every query. REPORT_M4.md section 4
is where a wrong estimate is made to choose a bad plan and the cost of it is measured.

### The memory budget

The same twenty five queries at B = 8 and at B = 4 return the same answers, row for row
(`python -m workload.answers`, compared with `--compare`). At B = 8 twenty of them do the same
I/O as at B = 32. The five that differ are the joins of three or more tables and the grouping
over a join, by 1 to 10 pages: W16 goes from 103 to 113 and W15 from 111 to 116. Joins that run at the
same time share the frames, so the lower ones get a smaller block and read their inner input
more often. The optimizer predicted 105 for W16 at B = 8: this is the gap ARCHITECTURE.md
section 5 names as the largest known error in the system.

## 4. Correctness

**Against an independent implementation, on every run of the tests.** `tests/test_workload.py`
builds the database with `workload/load.sql` through the prompt, runs all twenty five queries at
B = 4, 8 and 32, and compares each answer with the Bonus Project 1 engine evaluating the same
logical plan in memory. That engine shares no code with the storage engine, the executor or the
optimizer. A query with ORDER BY must match row for row in order; any other must match as a bag.
All seventy five comparisons pass.

**Against PostgreSQL.** All twenty five answers are the same. PostgreSQL loaded the same six CSV
files, ran the same twenty five query texts, and wrote each answer to a file; this system did
the same; the two sets of files were compared query by query. From the project root:

```
createdb --template=template0 --locale=C --encoding=UTF8 workload
psql -d workload -f workload/postgres_load.sql        the same six tables from the same CSV files
psql -d workload -f workload/postgres_queries.sql     writes workload/postgres/w01.csv ... w25.csv
python -m workload.answers --db wdb                   writes workload/answers/w01.csv ... w25.csv
python -m workload.answers --compare workload/answers workload/postgres
```

The last command prints one line per query, `same` or `DIFFERENT`, and a count. Values are put
in one form first: an integer as written, any other number to four decimal places, because this
system prints an average that way and PostgreSQL prints sixteen (W08: 2482.7846 here,
2482.7846153846153846 there). `--locale=C` makes PostgreSQL order strings byte by byte, as this
system does.

| | |
|---|---|
| PostgreSQL version | PostgreSQL 17.12 (Debian 17.12-1.pgdg13+1) on x86_64-pc-linux-gnu, 64-bit |
| Where it ran | the official `postgres:17` image in a Docker container on my laptop, on 2026-11-15; database `workload`, collation C, encoding UTF8 |
| Rows loaded | Customer 300, Orders 2,000, Item 3,000, Product 200, Supplier 30, Supply 500: the counts of section 0 |
| Rows returned per query | the same as the "result rows" column of section 2, for all twenty five |
| Result of the comparison | `25 of 25 the same` |

This was not run on the OpenStack instance. The five commands above are all it takes there: they
use nothing but `createdb`, `psql` and the files in `workload/`.

## 5. Sources

- Hellerstein, Stonebraker and Hamilton, *Architecture of a Database System*, Foundations and
  Trends in Databases, 2007. Its picture of a relational query processor, a parser, a rewriter,
  an optimizer and an executor over a storage manager, is the pipeline every query in section 2
  went through. Two things in my system follow it directly. Rewriting is a stage of its own in
  front of the cost-based choice (`optimizer/rewrite.py` runs to completion before
  `planner.py` costs anything), which is why `opt explain --rewrite` can show the plan before
  and after the rules. And the prompt is only wiring: `minidb.py` parses nothing, plans nothing
  and touches no page, so the four module tools and the prompt cannot disagree.
- Leis, Gubichev, Mirchev, Boncz, Kemper and Neumann, *How Good Are Query Optimizers, Really?*,
  VLDB 2015. Their finding that cardinality errors grow with every join is W16 in section 3:
  1.35, then 2.4, then 11.5 at the root, with no single step badly wrong. It is why section 3
  reads the estimates inside the plans of W13 and W16 and does not stop at the q-error of the
  root, which is all the checkpoint asks to be reported.
- The PostgreSQL documentation, *SELECT*. https://www.postgresql.org/docs/current/sql-select.html
  The rules the workload was written under come from here: JOIN binds more tightly than the
  commas of a FROM list, INTERSECT binds more tightly than UNION and EXCEPT, and a LIMIT is only
  predictable under an ORDER BY that puts the rows in a unique order. Each is a place where my
  subset, as specified, and PostgreSQL would answer different questions from the same text.
