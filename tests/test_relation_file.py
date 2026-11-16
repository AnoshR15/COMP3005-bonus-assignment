"""The relation file loader (Section 4.1 of the assignment) and the data error category."""
import os
from tests.base import RATestCase, DATA
from source.relation_file import load_relations
from source.errors import DataError


class LoaderTests(RATestCase):
    files = ('data/example.ra', 'tests/data/quoted.ra', 'tests/data/numbers.ra')

    def test_column_types_come_from_the_data(self):
        self.assertEqual([a.typ for a in self.db['Employees'].schema], ['string', 'string', 'number', 'string'])
        self.assertEqual(self.db['Q'].schema[0].typ, 'string')      # '30' is a string, 30 would be a number

    def test_quoted_values(self):
        self.assertEqual(self.db['Q'].rows,
                         [("O'Brien", 1), ('a,b', 2), ('Bob)', 3), ('30', 4), ('x//y', 5), ('}', 6)])

    def test_duplicates_are_kept_on_load(self):
        """Changed by the move from sets to bags: a relation file is a bag, so a tuple written
        twice is stored twice. distinct collapses it."""
        self.assertEqual(self.db['Dup'].rows, [(1,), (1,)])
        self.assertEqual(self.db['N'].rows, [(1,), (1,), (1.5,)])    # 1 and 1.0 are one value, held twice
        self.assertEqual(self.rows('distinct(N)'), {(1,), (1.5,)})

    def test_int_and_float_are_both_numbers(self):
        self.assertEqual(self.rows('I union F'), {(1,), (1.5,)})

    def test_bad_files_are_data_errors(self):
        bad = os.path.join(DATA, 'bad')
        for name in sorted(os.listdir(bad)):
            with self.subTest(file=name):
                with self.assertRaises(DataError):
                    load_relations(os.path.join(bad, name))
