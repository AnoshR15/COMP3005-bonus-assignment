"""The engine as a library: the four layers import independently, none of them prints or opens a
file, and the operators pull tuples from whatever source they are given."""
import ast                      # the standard library's ast, used to read the source files
import os
import subprocess
import sys
import unittest
from tests.base import ROOT, RATestCase
from source.engine import evaluate, parse_query
from source.operators import Relation, SchemaAttr, Counters, NUMBER, select, join
from source.errors import NameErrorRA

# What importing each layer may bring in from the package, itself included.
LAYERS = {
    'tokenizer': {'tokenizer', 'errors'},
    'parser': {'parser', 'ast', 'errors'},
    'tree': {'tree', 'ast'},
    'operators': {'operators', 'ast', 'errors'},
}
# Library modules: only ra.py, the benchmark, the generator and the plot script may do I/O.
# The SQL front end, except catalog.py, whose job is to read the catalog file.
SQL_LIBRARY = ['sqlfront/' + m for m in ('errors', 'lexer', 'nodes', 'parser', 'translate', 'explain')]
LIBRARY = ['tokenizer', 'parser', 'ast', 'tree', 'operators', 'engine', 'condition', 'plan_json', 'errors']


class LayerTests(unittest.TestCase):

    def test_each_layer_imports_without_the_others(self):
        for layer, allowed in LAYERS.items():
            with self.subTest(layer=layer):
                code = (f'import sys, source.{layer}; '
                        "print(' '.join(sorted(m[7:] for m in sys.modules if m.startswith('source.'))))")
                r = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(set(r.stdout.split()), allowed)

    def test_the_library_never_prints_or_opens_a_file(self):
        for module in ['source/' + m for m in LIBRARY] + SQL_LIBRARY:
            with self.subTest(module=module):
                with open(os.path.join(ROOT, module + '.py'), encoding='utf8') as f:
                    tree = ast.parse(f.read())
                called = {n.func.id for n in ast.walk(tree)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                self.assertEqual(called & {'print', 'open', 'input'}, set())
                imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
                self.assertEqual(imported & {'sys', 'os', 'io', 'pathlib'}, set())


class CountingSource:
    """A data source that is not a dictionary of relations: rows are generated on demand, and it
    records how many it has handed out."""

    def __init__(self, sizes):
        self.sizes = sizes
        self.handed_out = 0

    def scan(self, name):
        if name not in self.sizes:
            raise NameErrorRA(f"Unknown relation '{name}'")

        def rows():
            for i in range(self.sizes[name]):
                self.handed_out += 1
                yield (i, i % 3)

        return Relation(name, [SchemaAttr(name, 'a', NUMBER), SchemaAttr(name, 'b', NUMBER)], rows())


class DataSourceTests(RATestCase):

    def test_operators_pull_from_the_source_they_are_given(self):
        source = CountingSource({'R': 6, 'S': 3})
        result = evaluate(parse_query('project[R.a, S.a](R join[R.b = S.a] S)'), source)
        self.assertEqual(sorted(result.rows), [(0, 0), (1, 1), (2, 2), (3, 0), (4, 1), (5, 2)])

    def test_rows_are_pulled_one_at_a_time_not_collected_first(self):
        source = CountingSource({'R': 1000})
        result = evaluate(parse_query('project[a](select[a >= 10](R))'), source)
        self.assertEqual(source.handed_out, 0)              # building the plan reads nothing
        rows = iter(result.rows)
        self.assertEqual(next(rows), (10,))
        self.assertEqual(source.handed_out, 11)             # rows 0 to 10, and no further
        self.assertEqual(next(rows), (11,))
        self.assertEqual(source.handed_out, 12)

    def test_errors_do_not_wait_for_the_first_row(self):
        source = CountingSource({'R': 5})
        with self.assertRaises(NameErrorRA):
            evaluate(parse_query('select[Nope = 1](R)'), source)
        with self.assertRaises(NameErrorRA):
            evaluate(parse_query('R join[R.a = T.a] T'), source)
        self.assertEqual(source.handed_out, 0)

    def test_operators_accept_any_iterable_of_rows(self):
        schema = [SchemaAttr('R', 'a', NUMBER)]
        c = Counters()
        out = select(Relation('R', schema, iter([(1,), (2,), (2,)])), parse_query('select[a=2](R)').arg, c)
        self.assertEqual(list(out.rows), [(2,), (2,)])
        self.assertEqual(c.select_examined, 3)
        s = Relation('S', [SchemaAttr('S', 'a', NUMBER)], (row for row in [(2,), (2,)]))
        r = Relation('R', schema, (row for row in [(1,), (2,)]))
        pairs = join(r, s, parse_query('R join[R.a=S.a] S').arg, c)
        self.assertEqual(list(pairs.rows), [(2, 2), (2, 2)])
        self.assertEqual(c.join_compared, 4)
