"""Base class for the test modules.

Run the suite from the project root with `python -m unittest -v`. A test class names the
relation files it needs in `files` (paths relative to the project root, mostly under tests/data)
and setUpClass loads them once into self.db.
"""
import os
import unittest

from source.tokenizer import Tokenizer
from source.parser import Parser
from source.tree import tree
from source.engine import evaluate
from source.relation_file import load_relations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'tests', 'data')


class RATestCase(unittest.TestCase):
    files = ()

    @classmethod
    def setUpClass(cls):
        cls.db = {}
        for name in cls.files:
            cls.db.update(load_relations(os.path.join(ROOT, name)))

    def tokens(self, query):
        """(kind, value) pairs, without the EOF token."""
        return [(t.kind, t.value) for t in Tokenizer(query).scan() if t.kind != 'EOF']

    def parse(self, query):
        return Parser(Tokenizer(query).scan()).parse()

    def tree(self, query):
        return tree(self.parse(query))

    def evaluate(self, query, counters=None):
        return evaluate(self.parse(query), self.db, counters)

    def rows(self, query):
        return self.evaluate(query).rows

    def schema(self, query):
        return [a.qualified() for a in self.evaluate(query).schema]
