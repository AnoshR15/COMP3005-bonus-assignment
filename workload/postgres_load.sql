-- The same six tables and the same CSV files, for PostgreSQL. Run from the project root:
--     createdb --template=template0 --locale=C workload
--     psql -d workload -f workload/postgres_load.sql
-- --locale=C makes PostgreSQL order strings byte by byte, as this system does.

DROP TABLE IF EXISTS Customer, Orders, Item, Product, Supplier, Supply;

CREATE TABLE Customer (cid INT, name VARCHAR(16), city VARCHAR(12), country VARCHAR(10), segment INT, note VARCHAR(60));
CREATE TABLE Orders (oid INT, cid INT, odate INT, shipdate INT, status INT, total INT, note VARCHAR(60));
CREATE TABLE Item (oid INT, lineno INT, pid INT, qty INT, price INT, note VARCHAR(40));
CREATE TABLE Product (pid INT, pname VARCHAR(20), category INT, brand INT, weight INT, note VARCHAR(60));
CREATE TABLE Supplier (sid INT, sname VARCHAR(16), country VARCHAR(10));
CREATE TABLE Supply (pid INT, sid INT, cost INT);

\copy Customer FROM 'workload/data/Customer.csv' WITH (FORMAT csv)
\copy Orders FROM 'workload/data/Orders.csv' WITH (FORMAT csv)
\copy Item FROM 'workload/data/Item.csv' WITH (FORMAT csv)
\copy Product FROM 'workload/data/Product.csv' WITH (FORMAT csv)
\copy Supplier FROM 'workload/data/Supplier.csv' WITH (FORMAT csv)
\copy Supply FROM 'workload/data/Supply.csv' WITH (FORMAT csv)
