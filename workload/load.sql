-- The order database of the integration workload, built through the prompt.
--     python -m workload.build                              writes the six CSV files
--     python minidb.py --db wdb --file workload/load.sql    creates, loads and analyzes

CREATE TABLE Customer (cid INT, name VARCHAR(16), city VARCHAR(12), country VARCHAR(10), segment INT, note VARCHAR(60));
CREATE TABLE Orders (oid INT, cid INT, odate INT, shipdate INT, status INT, total INT, note VARCHAR(60));
CREATE TABLE Item (oid INT, lineno INT, pid INT, qty INT, price INT, note VARCHAR(40));
CREATE TABLE Product (pid INT, pname VARCHAR(20), category INT, brand INT, weight INT, note VARCHAR(60));
CREATE TABLE Supplier (sid INT, sname VARCHAR(16), country VARCHAR(10));
CREATE TABLE Supply (pid INT, sid INT, cost INT);

LOAD Customer FROM 'workload/data/Customer.csv';
LOAD Orders FROM 'workload/data/Orders.csv';
LOAD Item FROM 'workload/data/Item.csv';
LOAD Product FROM 'workload/data/Product.csv';
LOAD Supplier FROM 'workload/data/Supplier.csv';
LOAD Supply FROM 'workload/data/Supply.csv';

ANALYZE;
