"""The execution engine: every operator, the counters, and the memory budget.

The checklist of Section 6 of the Module 2 specification is the class ChecklistTests."""
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from tests.base import ROOT
from tests.execbase import ExecTestCase, TABLES, scan, join, bag
from executor import groupby
from executor.context import ExecError, BudgetError, PlanError, hash_key
from executor.joins import HashJoin
from executor.run import Execution, format_row
from store.db import Database

JOINS = ('nested_loop', 'sort_merge', 'hash')


def sort(keys, child):
    return {'op': 'sort', 'keys': keys, 'child': scan(child) if isinstance(child, str) else child}


def node(op, child, **fields):
    return {'op': op, **fields, 'child': scan(child) if isinstance(child, str) else child}


def group(mode, keys, aggregates, child):
    return node('group_by', child, mode=mode, keys=keys, aggregates=aggregates)


def by_label(execution, label):
    return next(op for _, op in execution.operators() if op.label == label)


class ChecklistTests(ExecTestCase):

    def test_the_sort_example_reproduces(self):
        """N = 8 pages, B = 3: three runs of 3, 3 and 2 pages; merge two and carry the third;
        merge the last two while streaming. 22 reads and 14 writes in three passes."""
        self.assertEqual(self.pages['R'], 8)
        rows, e = self.run_plan(sort([['R.a', 'asc']], 'R'), frames=3)
        self.assertEqual([r[0] for r in rows], list(range(2040)))
        self.assertEqual(e.totals(), {'tuples_out': 2040, 'comparisons': e.totals()['comparisons'],
                                      'page_reads': 22, 'page_writes': 14})
        self.assertEqual(by_label(e, 'seqscan R').counters()['page_reads'], 8)      # pass 0 reads
        s = by_label(e, 'sort')
        self.assertEqual((s.c.page_reads, s.c.page_writes), (6 + 8, 8 + 6))         # passes 1 and 2; 0 and 1
        self.assertEqual(s.details(), {'run_pages': 3, 'fan_in': 2, 'final_fan_in': 2,
                                       'initial_runs': 3, 'passes': 3})

    def test_the_duplicate_key_example_returns_four_rows(self):
        """R.b = [1, 2, 2, 3] and S.b = [2, 2, 4]: each 2 on the left meets each 2 on the right."""
        for kind in JOINS:
            with self.subTest(join=kind):
                rows = self.rows(join(kind, 'L4.b = R3.b', 'L4', 'R3'))
                self.assertEqual(rows, [(2, 2)] * 4)

    def test_all_three_joins_return_the_same_bag(self):
        results = [self.assertSameBag(join(kind, 'R.b = S.b', 'R', 'S'), frames=(4, 9, 64)) for kind in JOINS]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertEqual(sum(results[0].values()), 2040 * 5)

    def test_block_nested_loop_compares_every_pair_exactly_once(self):
        for condition in ('R.b = S.b', 'R.b < S.b', 'R.a = 1 and S.b = 2'):
            for frames in (4, 7, 64):
                with self.subTest(condition=condition, frames=frames):
                    _, e = self.run_plan(join('nested_loop', condition, 'R', 'S'), frames)
                    self.assertEqual(by_label(e, 'nested_loop_join').c.comparisons, 2040 * 300)

    def test_every_plan_runs_in_four_frames(self):
        deep = join('hash', 'R.b = S.b',
                    join('nested_loop', 'R.c = W.k', node('filter', 'R', condition='R.c < 60'), 'W'), 'S')
        plans = {
            'join of joins': deep,
            'sort over a join': sort([['S.d', 'desc'], ['R.a', 'asc']], join('hash', 'R.b = S.b', 'R', 'S')),
            'group over sort over join': group('hash', ['S.b'], [['count', '*'], ['max', 'R.a']],
                                               sort([['R.a', 'asc']], join('sort_merge', 'R.b = S.b', 'R', 'S'))),
            'three nested loops': join('nested_loop', 'V.k = S.b',
                                       join('nested_loop', 'W.k = V.k', node('filter', 'W', condition='W.k < 9'), 'V'),
                                       'S'),
            'nested loop with a join inside': join('nested_loop', 'L4.b < S.b', 'L4',
                                                   join('hash', 'S.b = R3.b', 'S', 'R3')),
            'distinct over union over joins': node('distinct', {
                'op': 'union_all',
                'left': node('project', join('sort_merge', 'R.b = S.b', 'R', 'S'), columns=['R.b']),
                'right': node('project', join('hash', 'R.b = S.b', 'R', 'S'), columns=['S.b'])}, mode='hash'),
            'set operators over sorts': {
                'op': 'except',
                'left': {'op': 'intersect', 'left': node('project', sort([['R.a', 'desc']], 'R'), columns=['R.b']),
                         'right': node('project', 'S', columns=['S.b'])},
                'right': node('project', node('filter', 'S', condition='S.b > 20'), columns=['S.b'])},
            'hash join of two hash joins': join('hash', 'R.b = V.k', join('hash', 'R.b = S.b', 'R', 'S'),
                                                join('hash', 'V.k = L4.b', 'V', 'L4')),
            'sort of a sort': sort([['R.b', 'asc']], sort([['R.a', 'desc']], 'R')),
            'hash group of hash group': group('hash', ['R.b'], [['count', '*']],
                                              node('distinct', node('project', 'R', columns=['R.b', 'R.a']),
                                                   mode='hash')),
        }
        for name, plan in plans.items():
            with self.subTest(plan=name):
                rows, e = self.run_plan(plan, frames=4)         # run_plan checks the peak and the clean-up
                self.assertEqual(bag(rows), bag(self.reference(plan)))
                self.assertEqual(e.peak_frames(), 4)

    def test_a_join_whose_output_is_larger_than_both_inputs(self):
        plan = join('hash', 'T.x = U.x', 'T', node('rename', 'T', to='U'))
        for kind in JOINS:
            plan['op'] = kind + '_join'
            with self.subTest(join=kind):
                rows = self.rows(plan, frames=5)
                self.assertEqual(len(rows), 4 * 150 * 150)              # 600 rows in, 90,000 out
        self.assertEqual(bag(rows), bag(self.reference(plan)))

    def test_an_empty_input_through_every_operator(self):
        e2 = node('rename', 'E', to='E2')
        plans = [scan('E'), node('filter', 'E', condition='E.a > 1'), node('project', 'E', columns=['E.b']),
                 sort([['E.a', 'asc']], 'E'), node('limit', 'E', count=3),
                 node('distinct', 'E', mode='sort'), node('distinct', 'E', mode='hash'),
                 group('sort', ['E.a'], [['count', '*'], ['sum', 'E.b']], 'E'),
                 group('hash', ['E.a'], [['count', '*'], ['sum', 'E.b']], 'E')]
        for kind in JOINS:
            plans += [join(kind, 'E.a = E2.a', 'E', e2), join(kind, 'E.a = R.a', 'E', 'R'),
                      join(kind, 'R.a = E.a', 'R', 'E')]
        for op in ('union_all', 'union', 'intersect', 'except'):
            plans += [{'op': op, 'left': scan('E'), 'right': e2}]
        for plan in plans:
            with self.subTest(plan=plan['op']):
                self.assertEqual(self.rows(plan), [])
        # union and except with one empty side still return the other side
        ints = node('project', 'R3', columns=['R3.b'])
        two = node('project', 'E', columns=['E.a'])
        self.assertEqual(sorted(self.rows({'op': 'union', 'left': two, 'right': ints})), [(2,), (4,)])
        self.assertEqual(sorted(self.rows({'op': 'except', 'left': ints, 'right': two})), [(2,), (4,)])
        self.assertEqual(self.rows({'op': 'intersect', 'left': ints, 'right': two}), [])

    def test_group_by_on_an_empty_input(self):
        for mode in ('sort', 'hash'):
            with self.subTest(mode=mode):
                # No grouping columns: exactly one row, COUNT is 0.
                self.assertEqual(self.rows(group(mode, [], [['count', '*']], 'E')), [(0,)])
                # ... and any other aggregate is undefined, which is an error.
                message = self.fails(group(mode, [], [['count', '*'], ['sum', 'E.b']], 'E'))
                self.assertIn('sum(E.b) of an empty input is undefined', message)
                # With grouping columns: no groups, no rows.
                self.assertEqual(self.rows(group(mode, ['E.a'], [['count', '*'], ['sum', 'E.b']], 'E')), [])

    def test_nothing_is_left_behind(self):
        """run_plan checks pins, reserved frames and temporary files after every plan. Here: after
        a plan is abandoned half way by a limit, and after a plan fails."""
        big = join('hash', 'R.b = S.b', sort([['R.a', 'asc']], 'R'), 'S')
        rows, e = self.run_plan(node('limit', big, count=7), frames=4)
        self.assertEqual(len(rows), 7)
        self.assertGreater(e.db.disk.temps_created, 0)                  # it did use temporary files
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'tmp')))
        self.fails(group('sort', [], [['min', 'E.a']], node('filter', big | {'right': scan('E')},
                                                            condition='R.a > 0')) | {})
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'tmp')))


class SortTests(ExecTestCase):

    def test_keys_directions_and_strings(self):
        rows = self.rows(sort([['S.b', 'desc'], ['S.d', 'asc']], 'S'))
        self.assertEqual(rows, sorted(TABLES['S'][1], key=lambda r: (-r[0], r[1])))
        rows = self.rows(sort([['T.s', 'asc'], ['T.x', 'desc']], 'T'), frames=3)
        self.assertEqual(rows, sorted(TABLES['T'][1], key=lambda r: (r[1], -r[0])))

    def test_strings_order_by_their_utf8_bytes(self):
        db_dir = tempfile.mkdtemp()
        db = Database(db_dir, frames=4)
        heap = db.create_table('U', [('s', 'VARCHAR', 12)])
        words = ['zebra', 'Zebra', 'éclair', 'eclair', 'apple', 'Ωmega', '10', '9', '']
        for w in words:
            heap.insert((w,))
        db.close()
        db = Database(db_dir, frames=3)
        rows = list(Execution(db, sort([['U.s', 'asc']], 'U')).rows())
        db.disk.close()
        self.assertEqual([r[0] for r in rows], sorted(words, key=lambda w: w.encode('utf-8')))

    def test_number_of_passes_matches_the_formula(self):
        """1 + ceil(log(ceil(N / B)) / log(B - 1)), with N = 8 and N = 30 pages."""
        for table, frames in [('R', 3), ('R', 4), ('R', 5), ('R', 8), ('W', 3), ('W', 4), ('W', 6), ('W', 16)]:
            n = self.pages[table]
            runs = math.ceil(n / frames)
            expected = 1 + (math.ceil(math.log(runs) / math.log(frames - 1) - 1e-9) if runs > 1 else 0)
            with self.subTest(pages=n, frames=frames):
                key = 'R.a' if table == 'R' else 'W.pad'
                _, e = self.run_plan(sort([[key, 'asc']], table), frames)
                d = by_label(e, 'sort').details()
                self.assertEqual((d['initial_runs'], d['passes']), (runs, expected))

    def test_an_input_that_fits_is_sorted_without_writing(self):
        rows, e = self.run_plan(sort([['R.a', 'desc']], 'R'), frames=9)
        self.assertEqual([r[0] for r in rows], list(range(2039, -1, -1)))
        self.assertEqual(e.totals()['page_reads'], 8)
        self.assertEqual(e.totals()['page_writes'], 0)
        self.assertEqual(by_label(e, 'sort').details()['passes'], 1)

    def test_sort_of_twenty_times_b_pages_at_b_4(self):
        db_dir = tempfile.mkdtemp()
        db = Database(db_dir, frames=16)
        heap = db.create_table('Big', [('a', 'INT', None), ('b', 'INT', None), ('c', 'INT', None)])
        n = 255 * 80
        for i in range(n):
            heap.insert(((i * 7919) % n, i, 0))
        self.assertEqual(heap.info.num_pages, 80)                       # 20 x B
        db.close()
        db = Database(db_dir, frames=4)
        e = Execution(db, sort([['Big.a', 'asc']], 'Big'))
        self.assertEqual([r[0] for r in e.rows()], list(range(n)))
        self.assertEqual(db.pool.high_water, 4)
        self.assertEqual(by_label(e, 'sort').details()['initial_runs'], 20)
        self.assertEqual(by_label(e, 'sort').details()['passes'], 4)     # 20 runs -> 7 -> 3 -> stream
        self.assertEqual((db.pool.pinned, len(db.pool.reserved), db.disk.live_temps), (0, 0, 0))
        db.disk.close()

    def test_comparisons_are_counted(self):
        _, e = self.run_plan(sort([['R.a', 'asc']], 'R'), frames=3)
        comparisons = e.totals()['comparisons']
        self.assertGreater(comparisons, 2040 * 9)       # about n log2 n / 1.1
        self.assertLess(comparisons, 2040 * 12)

    def test_varchar_order_is_byte_wise_on_utf8(self):
        """Section 1.3. The engine compares Python strings, and Python orders strings by code
        point. UTF-8 is built so that the encoded bytes sort the same way, and this test holds
        one against the other: characters of one, two, three and four bytes, a combining mark,
        a prefix of another value, and the empty string."""
        values = ['z', 'é', 'a', 'Z', '￿', '\U00010000', 'ab', '', 'à', 'aa']
        with tempfile.TemporaryDirectory() as db_dir:
            db = Database(db_dir, frames=8)
            heap = db.create_table('N', [('s', 'VARCHAR', 10)])
            for value in values:
                heap.insert((value,))
            db.close()
            db = Database(db_dir, frames=4)
            rows = list(Execution(db, sort([['N.s', 'asc']], 'N')).rows())
            db.disk.close()
        self.assertEqual([r[0] for r in rows], sorted(values, key=lambda s: s.encode('utf-8')))


class JoinTests(ExecTestCase):

    def test_nested_loop_page_reads_match_the_formula(self):
        """M + ceil(M / (B - 2)) * N, outer W (30 pages), inner V (10 pages)."""
        m, n = self.pages['W'], self.pages['V']
        for frames in (4, 5, 8, 17, 32):
            with self.subTest(frames=frames):
                _, e = self.run_plan(join('nested_loop', 'W.k = V.k', 'W', 'V'), frames)
                self.assertEqual(e.totals()['page_reads'], m + math.ceil(m / (frames - 2)) * n)
                self.assertEqual(e.totals()['page_writes'], 0)
                d = by_label(e, 'nested_loop_join').details()
                self.assertEqual((d['block_frames'], d['blocks']), (frames - 2, math.ceil(m / (frames - 2))))

    def test_nested_loop_accepts_any_condition(self):
        plan = join('nested_loop', 'S.b < R3.b and not S.d = \'v1\' or S.b = 59', 'S', 'R3')
        self.assertSameBag(plan)
        for kind in ('sort_merge', 'hash'):
            message = self.fails(join(kind, 'S.b < R3.b', 'S', 'R3'), error=PlanError)
            self.assertIn('needs equalities between a left column and a right column', message)

    def test_an_inner_input_that_is_not_a_scan_is_spooled_once(self):
        inner = node('filter', 'V', condition='V.k < 20')
        plan = join('nested_loop', 'W.k = V.k', 'W', inner)
        rows, e = self.run_plan(plan, frames=4)
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        d = by_label(e, 'nested_loop_join').details()
        self.assertTrue(d['inner_spooled'])
        self.assertEqual(by_label(e, 'filter').c.comparisons, 190)          # V was filtered once, not 15 times
        self.assertEqual(by_label(e, 'seqscan V').c.page_reads, self.pages['V'])
        nlj = by_label(e, 'nested_loop_join')
        self.assertEqual(nlj.c.page_writes, d['inner_pages'])
        self.assertEqual(nlj.c.page_reads, d['blocks'] * d['inner_pages'])

    def test_a_renamed_table_is_rescanned_not_spooled(self):
        plan = join('nested_loop', 'W.k = x.k', 'W', node('rename', 'V', to='x'))
        rows, e = self.run_plan(plan, frames=4)
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        d = by_label(e, 'nested_loop_join').details()
        self.assertFalse(d['inner_spooled'])
        self.assertEqual(e.totals()['page_writes'], 0)
        m, n = self.pages['W'], self.pages['V']
        self.assertEqual(e.totals()['page_reads'], m + math.ceil(m / 2) * n)

    def test_a_nested_loop_join_without_a_condition_is_a_cross_product(self):
        plan = {'op': 'nested_loop_join', 'left': scan('L4'), 'right': scan('R3')}
        rows, e = self.run_plan(plan)
        self.assertEqual(sorted(rows), sorted((a, b) for a in (1, 2, 2, 3) for b in (2, 2, 4)))
        self.assertEqual(by_label(e, 'nested_loop_join').c.comparisons, 12)
        for kind in ('hash', 'sort_merge'):
            message = self.fails({'op': kind + '_join', 'left': scan('L4'), 'right': scan('R3')}, error=PlanError)
            self.assertIn("missing required field 'condition'", message)

    def test_sort_merge_cost_and_composite_keys(self):
        plan = join('sort_merge', 'W.k = V.k and W.pad = V.pad', 'W', 'V')
        self.assertEqual(self.rows(plan), [])                               # the pads never match
        plan = join('sort_merge', 'W.k = V.k', 'W', 'V')
        rows, e = self.run_plan(plan, frames=4)
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        d = by_label(e, 'sort_merge_join').details()
        self.assertEqual(d['left_sort']['final_fan_in'], 1)                 # each side ends as one run on disk
        m, n = self.pages['W'], self.pages['V']
        smj = by_label(e, 'sort_merge_join')
        self.assertGreaterEqual(smj.c.page_writes, m + n)                   # every page written at least once
        self.assertGreaterEqual(smj.c.page_reads, m + n)                    # and read back for the merge

    def test_hash_join_partitions_and_cost(self):
        plan = join('hash', 'W.k = V.k', 'W', 'V')
        rows, e = self.run_plan(plan, frames=16)
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        hj = by_label(e, 'hash_join')
        d = hj.details()
        self.assertEqual(d['partitions'], 15)                               # B - 1
        self.assertEqual(d['overflows'], [])
        m, n = self.pages['W'], self.pages['V']
        written = sum(d['left_partition_pages']) + sum(d['right_partition_pages'])
        self.assertEqual((hj.c.page_writes, hj.c.page_reads), (written, written))
        self.assertLessEqual(written, m + n + 2 * 15)                       # one part-filled page per partition
        self.assertEqual(e.totals()['page_reads'] + e.totals()['page_writes'], (m + n) + 2 * written)

    def test_a_partition_that_does_not_fit_is_split_again_then_joined_by_nested_loop(self):
        plan = join('hash', 'W.k = V.k', 'W', 'V')
        rows, e = self.run_plan(plan, frames=4)                             # 30 pages into 3 partitions of 3 frames
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        actions = [o['action'] for o in by_label(e, 'hash_join').details()['overflows']]
        self.assertIn('partitioned again', actions)
        levels = {o['level'] for o in by_label(e, 'hash_join').details()['overflows']}
        self.assertLessEqual(max(levels), HashJoin.MAX_LEVEL)

    def test_one_key_in_most_rows_ends_in_the_nested_loop(self):
        # H: 100 wide rows, 60 of them with k = 0. That is more than three pages of one key, and
        # with 4 frames the table has three. No hash function can split them.
        plan = join('hash', 'H.k = H2.k', 'H', node('rename', 'H', to='H2'))
        rows, e = self.run_plan(plan, frames=4)
        self.assertEqual(len(rows), 60 * 60 + 40)
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        overflows = by_label(e, 'hash_join').details()['overflows']
        self.assertEqual([o['action'] for o in overflows], ['partitioned again', 'partitioned again', 'nested loop'])
        self.assertEqual([o['level'] for o in overflows], [1, 2, HashJoin.MAX_LEVEL])
        self.assertEqual(bag(self.rows(plan, frames=3)), bag(rows))
        _, roomy = self.run_plan(plan, frames=16)                           # with 15 frames it simply fits
        self.assertEqual(by_label(roomy, 'hash_join').details()['overflows'], [])

    def test_two_columns_with_the_same_name_is_an_error_at_open(self):
        message = self.fails(join('hash', 'Emp.mgr = Emp.eid', 'Emp', 'Emp'), error=PlanError)
        self.assertIn("two columns named 'Emp.eid'", message)
        plan = join('hash', 'e.mgr = m.eid', node('rename', 'Emp', to='e'), node('rename', 'Emp', to='m'))
        plan = node('project', plan, columns=['e.name', ['m.name', 'boss']])
        rows, e = self.run_plan(plan)
        self.assertEqual(sorted(rows), [('Ann', 'Ann'), ('Bob', 'Ann'), ('Cy', 'Bob'), ('Di', 'Bob')])
        self.assertEqual(e.schema(), [['e.name', 'VARCHAR', 10], ['boss', 'VARCHAR', 10]])


class GroupAndDistinctTests(ExecTestCase):

    def test_both_modes_agree_with_the_reference(self):
        for mode in ('sort', 'hash'):
            for plan in [group(mode, ['R.b'], [['count', '*'], ['sum', 'R.a'], ['min', 'R.c'], ['max', 'R.a']], 'R'),
                         group(mode, ['S.b', 'S.d'], [['count', '*']], 'S'),
                         group(mode, ['T.s'], [['max', 'T.x'], ['min', 'T.s'], ['count', 'T.x']], 'T'),
                         group(mode, [], [['count', '*'], ['sum', 'R.a'], ['max', 'R.b']], 'R'),
                         node('distinct', 'T', mode=mode), node('distinct', 'R', mode=mode),
                         node('distinct', node('project', 'W', columns=['W.k']), mode=mode)]:
                with self.subTest(mode=mode, plan=plan['op'], keys=plan.get('keys')):
                    self.assertSameBag(plan, frames=(4, 6, 40))

    def test_output_names_and_avg(self):
        plan = group('hash', ['Emp.mgr'], [['count', '*'], ['avg', 'Emp.salary'], ['max', 'Emp.name'],
                                           ['sum', 'Emp.salary', 'total']], 'Emp')
        rows, e = self.run_plan(plan)
        self.assertEqual(e.schema(), [['Emp.mgr', 'INT'], ['count_star', 'INT'], ['avg_Emp_salary', 'REAL'],
                                      ['max_Emp_name', 'VARCHAR', 10], ['total', 'INT']])
        self.assertEqual(sorted(rows), [(1, 2, 400.0, 'Bob', 800), (2, 2, 450.0, 'Di', 900)])
        self.assertEqual(format_row(rows[0]), '[1, 2, 400.0000, "Bob", 800]')     # four decimal places
        thirds = self.rows(group('sort', [], [['avg', 'L4.b']], 'L4'))
        self.assertEqual(format_row(thirds[0]), '[2.0000]')

    def test_an_aggregate_can_be_sorted_and_filtered_on(self):
        plan = sort([['avg_Emp_salary', 'desc']],
                    node('filter', group('sort', ['Emp.mgr'], [['avg', 'Emp.salary']], 'Emp'),
                         condition='avg_Emp_salary > 100'))
        self.assertEqual(self.rows(plan), [(2, 450.0), (1, 400.0)])

    def test_hash_mode_spills_partitions_and_still_groups_correctly(self):
        plan = group('hash', ['R.a'], [['count', '*'], ['sum', 'R.c']], 'R')        # 2040 groups, 2 frames of table
        rows, e = self.run_plan(plan, frames=4)
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        d = by_label(e, 'group_by').details()
        self.assertGreater(d['spilled_rows'], 0)
        self.assertGreater(d['partitions'], 0)
        self.assertEqual(d['sort_fallbacks'], 0)
        g = by_label(e, 'group_by')
        self.assertGreater(g.c.page_writes, 0)
        _, e = self.run_plan(plan, frames=40)                                       # now the table is big enough
        self.assertEqual(by_label(e, 'group_by').c.page_writes, 0)
        self.assertEqual(by_label(e, 'group_by').details()['spilled_rows'], 0)

    def test_hash_mode_falls_back_to_sorting_after_max_level(self):
        plan = node('distinct', node('project', 'R', columns=['R.a', 'R.c']), mode='hash')
        saved = groupby.MAX_LEVEL
        groupby.MAX_LEVEL = 0                       # never partition: straight to the fallback
        try:
            rows, e = self.run_plan(plan, frames=4)
        finally:
            groupby.MAX_LEVEL = saved
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        self.assertGreater(by_label(e, 'distinct').details()['sort_fallbacks'], 0)

    def test_distinct_of_a_table_larger_than_memory(self):
        for mode in ('sort', 'hash'):
            with self.subTest(mode=mode):
                rows, e = self.run_plan(node('distinct', 'W', mode=mode), frames=4)     # 30 pages, all distinct
                self.assertEqual(len(rows), 570)
                self.assertEqual(len(set(rows)), 570)


class SetAndPipelineTests(ExecTestCase):

    def test_set_operators(self):
        left = node('project', 'R', columns=['R.b'])                # 0..49, many copies
        right = node('project', 'S', columns=['S.b'])               # 0..59, five copies each
        self.assertEqual(len(self.rows({'op': 'union_all', 'left': left, 'right': right})), 2040 + 300)
        self.assertEqual(sorted(self.rows({'op': 'union', 'left': left, 'right': right})), [(i,) for i in range(60)])
        self.assertEqual(sorted(self.rows({'op': 'intersect', 'left': left, 'right': right})),
                         [(i,) for i in range(50)])
        self.assertEqual(sorted(self.rows({'op': 'except', 'left': right, 'right': left})),
                         [(i,) for i in range(50, 60)])
        for op in ('union_all', 'union', 'intersect', 'except'):
            with self.subTest(op=op):
                self.assertSameBag({'op': op, 'left': scan('W'), 'right': scan('V')}, frames=(4, 7))
                plan = {'op': op, 'left': scan('W'), 'right': node('rename', 'W', to='W2')}
                self.assertSameBag(plan, frames=(4, 12))

    def test_set_operators_take_the_left_names_and_check_the_types(self):
        _, e = self.run_plan({'op': 'union', 'left': node('project', 'S', columns=['S.d']),
                              'right': node('project', 'Emp', columns=['Emp.name'])})
        self.assertEqual(e.schema(), [['S.d', 'VARCHAR', 10]])
        message = self.fails({'op': 'intersect', 'left': scan('S'), 'right': scan('R')}, error=PlanError)
        self.assertIn('the left input has 2 columns and the right has 3', message)
        message = self.fails({'op': 'except', 'left': scan('S'), 'right': node('rename', 'E', to='X')},
                             error=PlanError)
        self.assertIn('column 2 is VARCHAR on the left (S.d) and INT on the right (X.b)', message)

    def test_limit_stops_pulling(self):
        rows, e = self.run_plan(node('limit', 'R', count=5))
        self.assertEqual(len(rows), 5)
        self.assertEqual(by_label(e, 'seqscan R').c.tuples_out, 5)
        self.assertEqual(by_label(e, 'seqscan R').c.page_reads, 1)          # one page, not eight
        self.assertEqual(self.rows(node('limit', 'R', count=0)), [])
        self.assertEqual(len(self.rows(node('limit', 'S', n=1000))), 300)

    def test_filter_counts_one_comparison_per_row(self):
        plan = node('filter', 'R', condition='R.a > 500 and R.b = 7 or R.c < 3')
        rows, e = self.run_plan(plan)
        self.assertEqual(bag(rows), bag(self.reference(plan)))
        self.assertEqual(by_label(e, 'filter').counters(), {'tuples_out': len(rows), 'comparisons': 2040,
                                                            'page_reads': 0, 'page_writes': 0})

    def test_project_keeps_duplicates(self):
        rows = self.rows(node('project', 'T', columns=['T.s']))
        self.assertEqual(len(rows), 600)
        self.assertEqual(set(rows), {('a',), ('b',), ('c',)})

    def test_scan_reads_each_page_once_and_pins_one_at_a_time(self):
        rows, e = self.run_plan(scan('R'), frames=1)
        self.assertEqual(rows, TABLES['R'][1])                              # heap order
        self.assertEqual(e.totals(), {'tuples_out': 2040, 'comparisons': 0, 'page_reads': 8, 'page_writes': 0})
        self.assertEqual(e.peak_frames(), 1)


class BudgetAndCounterTests(ExecTestCase):

    def test_too_few_frames_is_an_error_that_says_how_many_are_needed(self):
        message = self.fails(join('hash', 'R.b = S.b', 'R', 'S'), frames=2, error=BudgetError)
        self.assertIn('cannot run in 2 frames; it needs at least 3', message)
        deep = sort([['R.a', 'asc']], join('nested_loop', 'R.b = S.b', 'R', 'S'))
        self.assertIn('needs at least 4', self.fails(deep, frames=3, error=BudgetError))

    def test_the_frames_are_divided_by_the_three_rules(self):
        plan = sort([['R.a', 'asc']], join('hash', 'R.b = S.b', join('nested_loop', 'R.c = W.k', 'R', 'W'), 'S'))
        _, e = self.run_plan(plan, frames=64)
        holds = {op.label: op.hold for _, op in e.operators()}
        # sort keeps 32 for its runs and lets the hash join hold 32 while it probes; the hash join
        # let the nested loop hold 32 while it partitioned, into 64 - 32 partitions.
        self.assertEqual(holds['hash_join'], 32)
        self.assertEqual(holds['nested_loop_join'], 32)
        self.assertEqual(by_label(e, 'sort').details()['run_pages'], 32)
        self.assertEqual(by_label(e, 'hash_join').details()['partitions'], 32)
        self.assertEqual(by_label(e, 'nested_loop_join').details()['block_frames'], 30)
        self.assertEqual(e.peak_frames(), 64)

    def test_counters_are_identical_from_run_to_run(self):
        plan = group('hash', ['S.d'], [['count', '*']],
                     join('hash', 'R.b = S.b', sort([['R.a', 'asc']], 'R'), node('distinct', 'S', mode='hash')))
        for frames in (4, 11):
            for policy in ('lru', 'clock'):
                with self.subTest(frames=frames, policy=policy):
                    first = self.run_plan(plan, frames, policy)[1].report()
                    second = self.run_plan(plan, frames, policy)[1].report()
                    self.assertEqual(first, second)

    def test_counters_do_not_depend_on_pythons_string_hashing(self):
        """Python hashes strings differently in every process unless told otherwise. The engine's
        own hash function must not."""
        self.assertEqual(hash_key(('abc', 5), 0), 11661793648763399524)
        self.assertNotEqual(hash_key(('abc', 5), 0), hash_key(('abc', 5), 1))
        self.assertEqual(hash_key((2,), 3), hash_key((2.0,), 3))
        plan_path = os.path.join(self.dir, 'p.json')
        with open(plan_path, 'w') as f:
            json.dump(join('hash', 'S.d = X.d', 'S', node('rename', 'S', to='X')), f)
        outputs = set()
        for seed in ('1', '2', '3'):
            r = subprocess.run([sys.executable, 'exec.py', 'trace', '--db', self.dir, '--plan', plan_path,
                                '--frames', '4'], cwd=ROOT, capture_output=True, text=True,
                               env={**os.environ, 'PYTHONHASHSEED': seed})
            outputs.add(r.stdout)
        self.assertEqual(len(outputs), 1)

    def test_page_counts_add_up_to_what_the_disk_manager_did(self):
        plan = join('sort_merge', 'W.k = V.k', 'W', node('distinct', 'V', mode='hash'))
        db = Database(self.dir, frames=5)
        e = Execution(db, plan)                     # reads the catalog: not part of any operator
        reads, writes = db.disk.reads, db.disk.writes
        list(e.rows())
        totals = e.totals()
        self.assertEqual(totals['page_reads'], db.disk.reads - reads)
        self.assertEqual(totals['page_writes'], db.disk.writes - writes)
        db.disk.close()

    def test_tuples_out_is_per_operator(self):
        plan = node('project', node('filter', join('hash', 'R.b = S.b', 'R', 'S'), condition='R.a < 100'),
                    columns=['R.a', 'S.d'])
        rows, e = self.run_plan(plan)
        out = {op.label: op.c.tuples_out for _, op in e.operators()}
        self.assertEqual(out, {'project': 500, 'filter': 500, 'hash_join': 10200, 'seqscan R': 2040,
                               'seqscan S': 300})
        self.assertEqual(e.totals()['tuples_out'], 500)                     # the root's


class PlanErrorTests(ExecTestCase):

    def rejected(self, plan):
        db = Database(self.dir, frames=4)
        reads = db.disk.reads
        try:
            with self.assertRaises(PlanError) as cm:
                Execution(db, plan)
            self.assertLessEqual(db.disk.reads - reads, db.disk.num_pages('catalog.db'))   # only the catalog
            return str(cm.exception)
        finally:
            db.disk.close()

    def test_messages_name_the_node_and_the_problem(self):
        cases = [
            ({'op': 'sort', 'child': scan('R')}, "Node 'sort' at $: missing required field 'keys'"),
            (node('filter', 'R', condition=5), "Node 'filter' at $: field 'condition' must be a string"),
            ({'op': 'merge_join', 'left': scan('R'), 'right': scan('S')},
             "Node 'merge_join' at $: unknown operator 'merge_join'"),
            (node('limit', node('project', {'op': 'seqscan'}, columns=['R.a']), count=1),
             "Node 'seqscan' at $.child.child: missing required field 'table'"),
            (node('distinct', 'R', mode='tree'), "Node 'distinct' at $: field 'mode' must be \"sort\" or \"hash\""),
            (sort([['R.a', 'up']], 'R'), "field 'keys': every key must be [column name, \"asc\" or \"desc\"]"),
            (group('hash', ['R.a'], [['median', 'R.b']], 'R'), "unknown aggregate 'median'"),
            (node('limit', 'R', count=-1), "field 'count' cannot be negative"),
            (node('filter', 'R', condition='R.a >', extra=1), "unexpected field 'extra'"),
            (join('hash', 'R.b = S.b', 'R', {'op': 'seqscan', 'table': 7}),
             "Node 'seqscan' at $.right: field 'table' must be a string"),
            ([], 'Node at $: a plan node must be an object'),
        ]
        for plan, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self.rejected(plan))

    def test_unknown_tables_and_columns_are_rejected_before_any_work(self):
        cases = [
            (join('hash', 'R.b = S.b', 'R', scan('Nope')), "Node 'seqscan' at $.right: unknown table 'Nope'"),
            (node('project', 'R', columns=['R.a', 'R.zzz']), "Node 'project' at $: unknown attribute 'R.zzz'"),
            (node('filter', 'R', condition='S.b = 1'), "Node 'filter' at $: field 'condition': unknown attribute 'S.b'"),
            (node('filter', 'R', condition='R.a = \'x\''), 'cannot compare number to string'),
            (node('filter', 'R', condition='R.a = = 3'), "field 'condition': expected"),
            (sort([['S.d', 'asc']], 'R'), "Node 'sort' at $: unknown attribute 'S.d'"),
            (join('sort_merge', 'R.b = S.d', 'R', 'S'), 'cannot compare number to string'),
            (group('sort', ['R.a'], [['sum', 'S.d']], 'S'), 'unknown attribute'),
            (group('sort', ['S.b'], [['sum', 'S.d']], 'S'), 'sum(S.d) needs an INT column'),
            (group('sort', ['S.b'], [['max', '*']], 'S'), 'max(*) is not defined'),
            (node('project', 'R', columns=['R.a', ['R.b', 'R.a']]), "two columns named 'R.a'"),
        ]
        for plan, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self.rejected(plan))


class CommandLineTests(ExecTestCase):

    def exec(self, command, plan, *args):
        path = os.path.join(self.dir, 'plan.json')
        with open(path, 'w') as f:
            f.write(plan if isinstance(plan, str) else json.dumps(plan))
        return subprocess.run([sys.executable, 'exec.py', command, '--db', self.dir, '--plan', path, *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_run_json(self):
        plan = node('project', join('hash', 'Emp.mgr = L4.b', 'Emp', 'L4'), columns=['Emp.eid', 'Emp.name'])
        r = self.exec('run', plan, '--frames', '4', '--json')
        self.assertEqual(r.returncode, 0, r.stdout)
        out = json.loads(r.stdout)
        self.assertEqual(out['schema'], [['Emp.eid', 'INT'], ['Emp.name', 'VARCHAR', 10]])
        self.assertEqual(sorted(out['rows']), [[1, 'Ann'], [2, 'Bob'], [3, 'Cy'], [3, 'Cy'], [4, 'Di'], [4, 'Di']])
        self.assertEqual(set(out['totals']), {'tuples_out', 'comparisons', 'page_reads', 'page_writes'})
        self.assertEqual(out['totals']['tuples_out'], 6)
        r = self.exec('run', group('sort', [], [['avg', 'Emp.salary']], 'Emp'), '--frames', '4', '--json')
        self.assertIn('[425.0000]', r.stdout)
        self.assertEqual(json.loads(r.stdout)['rows'], [[425.0]])
        r = self.exec('run', scan('E'), '--frames', '4', '--json')
        self.assertEqual(json.loads(r.stdout)['rows'], [])

    def test_trace_is_one_line_per_node_in_plan_order(self):
        r = self.exec('trace', join('hash', 'R.b = S.b', 'R', node('filter', 'S', condition='S.b < 10')),
                      '--frames', '8')
        lines = r.stdout.splitlines()
        self.assertEqual([line.split()[0] for line in lines], ['hash_join', 'seqscan', 'filter', 'seqscan'])
        self.assertEqual([len(line) - len(line.lstrip()) for line in lines], [0, 2, 2, 4])
        self.assertRegex(lines[0], r'^hash_join +tuples_out=\d+ +comparisons=\d+ +reads=\d+ +writes=\d+$')
        self.assertRegex(lines[1], r'^  seqscan R +tuples_out=2040 +comparisons=0 +reads=8 +writes=0$')

        def reads_and_writes(output):
            return [(int(line.split('reads=')[1].split()[0]), int(line.split('writes=')[1])) for line in output.splitlines()]

        # As in the example of the specification, a line's page counts include everything below
        # it: the join's line is its two inputs' reads plus what it read back from its partitions.
        below = reads_and_writes(r.stdout)
        own = reads_and_writes(self.exec('trace', join('hash', 'R.b = S.b', 'R', node('filter', 'S', condition='S.b < 10')),
                                         '--frames', '8', '--own').stdout)
        self.assertEqual(own[2], (0, 0))                                    # a filter reads nothing itself
        self.assertEqual(below[2], own[3])                                  # its line is its scan's
        self.assertEqual(below[1], own[1])                                  # a scan has nothing below it
        self.assertGreater(own[0][0], 0)                                    # the partitions, read back
        self.assertEqual(below[0], (sum(r for r, _ in own), sum(w for _, w in own)))
        totals = json.loads(self.exec('run', join('hash', 'R.b = S.b', 'R', node('filter', 'S', condition='S.b < 10')),
                                      '--frames', '8', '--json').stdout)['totals']
        self.assertEqual(below[0], (totals['page_reads'], totals['page_writes']))   # the top line is the plan
        r = self.exec('trace', scan('R'), '--frames', '8', '--json')
        self.assertEqual(json.loads(r.stdout)['peak_frames'], 1)

    def test_errors_are_one_line(self):
        for plan, expected in [('{"op": "seqscan", "table": }', 'error: the plan is not valid JSON: '),
                               ({'op': 'sort', 'child': scan('R')}, "error: Node 'sort' at $: missing required field 'keys'"),
                               (scan('Nope'), "error: Node 'seqscan' at $: unknown table 'Nope'"),
                               (sort([['R.a', 'asc']], 'R'), 'error: this plan cannot run in 2 frames; it needs at least 3')]:
            with self.subTest(expected=expected):
                r = self.exec('run', plan, '--frames', '2')
                self.assertEqual(r.returncode, 1)
                self.assertTrue(r.stdout.startswith(expected), r.stdout)
                self.assertEqual(len(r.stdout.strip().splitlines()), 1)
                self.assertNotIn('Traceback', r.stderr)
        self.assertIn('line 1, column 28', self.exec('run', '{"op": "seqscan", "table": }', '--frames', '4').stdout)

    def test_a_failure_after_rows_were_written_still_leaves_valid_json(self):
        """The right input of a union_all is opened when the left one is finished, so this plan
        fails after four rows are already out. They cannot be taken back; the JSON object is
        closed properly instead, with an "error" member, and the exit code says it failed."""
        plan = {'op': 'union_all', 'left': scan('L4'), 'right': group('hash', [], [['sum', 'E.b']], 'E')}
        r = self.exec('run', plan, '--frames', '4', '--json')
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)                                      # it parses
        self.assertEqual(out['rows'], [[1], [2], [2], [3]])
        self.assertEqual(out['error'], "Node 'group_by' at $.right: sum(E.b) of an empty input is undefined")
        self.assertEqual(set(out), {'schema', 'rows', 'error', 'totals'})
        self.assertEqual(r.stderr.strip(), 'error: ' + out['error'])
        r = self.exec('run', plan, '--frames', '4')                     # as text: the rows, then the one line
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines()[1:], ['1', '2', '2', '3', 'error: ' + out['error']])

    def test_an_error_while_running_is_still_one_line(self):
        """SUM of an empty input fails when the plan runs, not when it is built. Nothing may have
        been printed by then: not the schema line, not the opening of the JSON object."""
        plan = group('sort', [], [['count', '*'], ['sum', 'E.b']], 'E')
        for args in (['--json'], []):
            with self.subTest(args=args):
                r = self.exec('run', plan, '--frames', '4', *args)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(r.stdout.strip(),
                                 "error: Node 'group_by' at $: sum(E.b) of an empty input is undefined")
