"""Random plans against the reference engine.

Each plan is run at several small budgets and must (1) return the bag of rows the Bonus Project
1 engine returns for the same plan with no budget, (2) never hold more than B frames, and
(3) leave no pinned page, reserved frame or temporary file. The seeds are fixed, so a failure
can be reproduced:  python -m unittest tests.test_exec_fuzz

This is how two of the three frame-accounting bugs in union_all were found (DESIGN_LOG.md):
both needed a blocking operator on the right of a union_all under another blocking operator, at
particular budgets, a combination no hand-written test had.
"""
import json
import random
from tests.execbase import ExecTestCase, bag
from tests.plan_fuzz import random_plan
from sqlfront.explain import plan_tree

SEEDS = range(160)
BUDGETS = (4, 5, 8, 23)


class RandomPlanTests(ExecTestCase):

    def test_random_plans_match_the_reference_at_every_budget(self):
        ran = 0
        for seed in SEEDS:
            rng = random.Random(seed)
            plan = random_plan(rng, depth=rng.choice([2, 3, 3, 4]))
            expected = self.reference(plan)
            if len(expected) > 40000:
                continue
            expected = bag(expected)
            for frames in BUDGETS:
                with self.subTest(seed=seed, frames=frames):
                    policy = 'clock' if seed % 2 else 'lru'
                    try:
                        rows, _ = self.run_plan(json.loads(json.dumps(plan)), frames, policy)
                    except Exception:
                        print(f'\nseed {seed}, {frames} frames:\n{plan_tree(plan)}')
                        raise
                    self.assertEqual(bag(rows), expected, plan_tree(plan))
                    ran += 1
        self.assertGreater(ran, 600)
