"""The data generator used by the performance study."""
import os
import tempfile
from tests.base import RATestCase
from source.generate_data import generate
from source.relation_file import load_relations
from source.engine import evaluate
from source.operators import Counters


class GeneratorTests(RATestCase):

    def test_match_rate_sets_the_output_size_not_the_comparisons(self):
        with tempfile.TemporaryDirectory() as out:
            for rate, expected in [(0, 0), (0.1, 20), (0.5, 100), (1.0, 200)]:
                with self.subTest(match_rate=rate):
                    generate(200, 200, rate, out)
                    db = {}
                    for name in ('R.ra', 'S.ra'):
                        db.update(load_relations(os.path.join(out, name)))
                    c = Counters()
                    result = evaluate(self.parse('R join[R.b=S.b] S'), db, c)
                    self.assertEqual(c.join_compared, 200 * 200)
                    self.assertEqual(len(result.rows), expected)
