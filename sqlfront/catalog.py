"""The catalog as the SQL front end sees it: table name -> ordered list of typed columns.

The front end only ever calls get_table() and create_table(), on a copy of the schemas held in
memory. load_catalog() fills that copy from a database directory:

    DIR/catalog.db     a database of the storage engine: its catalog pages are read through a
                       small buffer pool (Module 1)
    DIR/catalog.json   otherwise, a plain description, handy for tests and for compiling SQL
                       against a schema that has no data yet:

    { "tables": { "Emp": [ ["eid", "INT"], ["name", "VARCHAR(20)"] ] } }
"""
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnDef:
    name: str
    kind: str               # 'INT' or 'VARCHAR'
    size: object = None     # n of VARCHAR(n); None for INT

    def type_text(self):
        return 'INT' if self.kind == 'INT' else f'VARCHAR({self.size})'


class Catalog:

    def __init__(self, tables=None):
        self.tables = dict(tables or {})        # name -> list of ColumnDef

    def get_table(self, name):
        """The table's columns in schema order, or None when there is no such table."""
        return self.tables.get(name)

    def create_table(self, name, columns):
        self.tables[name] = list(columns)

    def list_tables(self):
        return sorted(self.tables)


class CatalogError(Exception):
    pass


def parse_type(text):
    """'INT' or 'VARCHAR(20)' -> (kind, size). Scanned by hand."""
    t = text.strip().upper()
    if t == 'INT':
        return 'INT', None
    if t.startswith('VARCHAR(') and t.endswith(')') and t[8:-1].isdigit():
        size = int(t[8:-1])
        if 1 <= size <= 255:
            return 'VARCHAR', size
    raise CatalogError(f"'{text}' is not a column type; expected INT or VARCHAR(n) with n from 1 to 255")


def catalog_from_dict(data):
    tables = {}
    if not isinstance(data, dict) or not isinstance(data.get('tables'), dict):
        raise CatalogError('catalog.json must be an object with a "tables" object')
    for name, columns in data['tables'].items():
        defs = []
        for column in columns:
            if not isinstance(column, list) or len(column) != 2 or not all(isinstance(x, str) for x in column):
                raise CatalogError(f'table {name}: every column must be [name, type]')
            defs.append(ColumnDef(column[0], *parse_type(column[1])))
        tables[name] = defs
    return Catalog(tables)


def load_catalog(directory):
    from store.db import Database, is_database
    if is_database(directory):
        db = Database(directory, frames=4)
        try:
            return Catalog({name: [ColumnDef(*column) for column in db.catalog.get_table(name).columns]
                            for name in db.catalog.list_tables()})
        finally:
            db.disk.close()
    path = os.path.join(directory, 'catalog.json')
    try:
        with open(path, encoding='utf8') as f:
            data = json.load(f)
    except OSError as e:
        raise CatalogError(f'cannot read {path}: {e.strerror}') from None
    except json.JSONDecodeError as e:
        raise CatalogError(f'{path} is not valid JSON: {e.msg} at line {e.lineno}') from None
    return catalog_from_dict(data)
