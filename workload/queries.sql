-- The integration workload: twenty five queries over the order database of workload/load.sql.
-- Every query is also valid PostgreSQL with the same meaning, so the answers can be compared.
-- For that reason no query mixes commas with JOIN, no query mixes INTERSECT with UNION or EXCEPT
-- without parentheses, every LIMIT comes with an ORDER BY on a key that has no ties, and no
-- aggregate other than COUNT is taken over an input that can be empty.

-- W01 a string equality and a <>
SELECT cid, name, city FROM Customer WHERE country = 'Japan' AND segment <> 0;

-- W02 OR
SELECT oid, total FROM Orders WHERE total >= 4900 OR total <= 50;

-- W03 NOT over a parenthesised OR
SELECT oid FROM Orders WHERE NOT (status = 0 OR odate < 900);

-- W04 DISTINCT with ORDER BY
SELECT DISTINCT country, city FROM Customer ORDER BY country, city;

-- W05 ORDER BY two keys, one descending, with LIMIT
SELECT pid, pname, weight FROM Product WHERE category = 2 ORDER BY weight DESC, pid LIMIT 5;

-- W06 ORDER BY a column that is not selected
SELECT name FROM Customer ORDER BY cid DESC LIMIT 7;

-- W07 COUNT(*) over no rows: one row, 0
SELECT COUNT(*) FROM Item WHERE qty = 11;

-- W08 all five aggregates with no GROUP BY
SELECT MIN(total), MAX(total), SUM(total), AVG(total), COUNT(*) FROM Orders WHERE status = 2;

-- W09 GROUP BY one column
SELECT status, COUNT(*) AS n, MIN(odate), MAX(shipdate) FROM Orders GROUP BY status;

-- W10 GROUP BY two columns, HAVING on an aggregate
SELECT country, segment, COUNT(*) AS n FROM Customer GROUP BY country, segment HAVING COUNT(*) >= 12;

-- W11 grouping over a join, ordered by an aggregate
SELECT c.country, SUM(o.total) AS revenue, AVG(o.total) AS average
  FROM Customer c JOIN Orders o ON c.cid = o.cid
 WHERE o.status <> 0
 GROUP BY c.country
 ORDER BY revenue DESC;

-- W12 a two-table join, a filter on each side
SELECT c.name, o.oid, o.total FROM Customer c JOIN Orders o ON c.cid = o.cid
 WHERE c.city = 'Oslo' AND o.total > 4000;

-- W13 a comma join, with both correlated pairs in the WHERE
SELECT c.name, o.oid FROM Customer c, Orders o
 WHERE c.cid = o.cid AND o.odate > 900 AND o.shipdate > 900 AND c.city = 'Arica' AND c.country = 'Chile';

-- W14 three tables
SELECT o.oid, i.lineno, p.pname FROM Orders o JOIN Item i ON o.oid = i.oid JOIN Product p ON i.pid = p.pid
 WHERE o.status = 4 AND p.category = 0 AND i.qty >= 9;

-- W15 four tables
SELECT c.name, p.pname, i.qty
  FROM Customer c JOIN Orders o ON c.cid = o.cid JOIN Item i ON o.oid = i.oid JOIN Product p ON i.pid = p.pid
 WHERE c.segment = 4 AND p.brand = 3;

-- W16 five tables
SELECT s.sname, p.pname, i.price
  FROM Supplier s JOIN Supply y ON s.sid = y.sid JOIN Product p ON y.pid = p.pid
       JOIN Item i ON i.pid = p.pid JOIN Orders o ON o.oid = i.oid
 WHERE s.country = 'Kenya' AND y.cost > 90 AND o.status = 3;

-- W17 six tables
SELECT c.name, s.sname
  FROM Customer c JOIN Orders o ON c.cid = o.cid JOIN Item i ON o.oid = i.oid JOIN Product p ON i.pid = p.pid
       JOIN Supply y ON y.pid = p.pid JOIN Supplier s ON s.sid = y.sid
 WHERE c.city = 'Tokyo' AND s.country = 'Peru' AND i.qty = 1;

-- W18 a self join, with an inequality between the two copies
SELECT a.cid, b.cid FROM Customer a JOIN Customer b ON a.city = b.city
 WHERE a.cid < b.cid AND a.segment = 0 AND b.segment = 0;

-- W19 an equality and an inequality in one join condition
SELECT p.pid, y.sid FROM Product p JOIN Supply y ON p.pid = y.pid AND y.cost > p.weight WHERE p.brand < 2;

-- W20 a join on an inequality alone: only a nested loop can do it
SELECT a.sid, b.sid FROM Supplier a JOIN Supplier b ON a.sid < b.sid WHERE a.sid > 25;

-- W21 a subquery in FROM, joined to a table
SELECT c.name, t.orders, t.spent
  FROM (SELECT cid, COUNT(*) AS orders, SUM(total) AS spent FROM Orders GROUP BY cid) AS t
       JOIN Customer c ON t.cid = c.cid
 WHERE t.orders >= 12;

-- W22 UNION ALL keeps every row
SELECT country FROM Customer UNION ALL SELECT country FROM Supplier;

-- W23 UNION on the same inputs removes the duplicates
SELECT country FROM Customer UNION SELECT country FROM Supplier;

-- W24 INTERSECT
SELECT cid FROM Orders WHERE total > 4950 INTERSECT SELECT cid FROM Orders WHERE status = 4;

-- W25 EXCEPT, with parentheses around a UNION
(SELECT pid FROM Product WHERE category = 1 UNION SELECT pid FROM Supply WHERE cost < 3)
EXCEPT SELECT pid FROM Item WHERE qty = 10;
