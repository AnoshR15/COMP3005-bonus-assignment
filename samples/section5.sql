-- The queries of Module 3, Section 5, in order. Against the sample database:
--     python sql.py compile --file samples/section5.sql --json        the eight logical plans
--     python minidb.py --file samples/section5.sql                    run them

-- 5.1 a plain filter and projection
SELECT name, age FROM Emp WHERE age > 30;

-- 5.2 a join with aliases
SELECT e.name, d.title
  FROM Emp e JOIN Dept d ON e.did = d.did
 WHERE d.budget > 1000;

-- 5.3 comma join
SELECT * FROM Emp e, Dept d WHERE e.did = d.did;

-- 5.4 grouping with HAVING
SELECT did, COUNT(*) AS n, AVG(salary) AS avg_sal
  FROM Emp
 WHERE age > 25
 GROUP BY did
HAVING COUNT(*) > 5
 ORDER BY n DESC
 LIMIT 10;

-- 5.5 self join
SELECT e.name, m.name FROM Emp e JOIN Emp m ON e.mgr = m.eid;

-- 5.6 subquery in FROM
SELECT t.did, t.n
  FROM (SELECT did, COUNT(*) AS n FROM Emp GROUP BY did) AS t
 WHERE t.n > 3;

-- 5.7 the two unions
SELECT city FROM Emp UNION ALL SELECT city FROM Dept;
SELECT city FROM Emp UNION SELECT city FROM Dept;
