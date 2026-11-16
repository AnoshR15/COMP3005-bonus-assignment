"""Bag semantics: duplicates survive every operator except distinct and the set versions of
union, intersect and minus. Also qualified names through every operator."""
from tests.base import RATestCase
from source.ast import RelationRef, BinaryOp
from source.engine import evaluate, MemorySource
from source.errors import NameErrorRA, ParseError

BAGS = 'tests/data/bags.ra'              # P = [1, 1, 1, 2, 3]   Q = [1, 1, 2, 2, 4]


class BagTests(RATestCase):
    files = (BAGS, 'tests/data/company.ra')

    def all_version(self, op):
        tree = BinaryOp(op, RelationRef('P'), RelationRef('Q'), 'all')
        return sorted(evaluate(tree, MemorySource(self.db)).rows)

    def test_select_and_rename_keep_duplicates(self):
        self.assertEqual(self.bag('select[v<2](P)'), [(1,), (1,), (1,)])
        self.assertEqual(self.bag('rename[X](P)'), [(1,), (1,), (1,), (2,), (3,)])

    def test_distinct_keeps_one_copy_of_each_row(self):
        self.assertEqual(self.rows('distinct(P)'), {(1,), (2,), (3,)})
        self.assertEqual(self.tree('distinct(P)'), 'Distinct\n└── Relation(P)')

    def test_distinct_compares_whole_rows(self):
        q = 'project[Emp.DID, Dept.DName](Emp join[Emp.DID=Dept.DID] Dept)'
        self.assertEqual(self.bag(q), [('D1', 'Engineering'), ('D1', 'Engineering'), ('D2', 'HR')])
        self.assertEqual(self.rows(f'distinct({q})'), {('D1', 'Engineering'), ('D2', 'HR')})

    def test_product_and_join_multiply_duplicates(self):
        self.assertEqual(len(self.bag('P times rename[Q2](Q)')), 5 * 5)
        # 1 pairs 3 x 2 times, 2 pairs 1 x 2 times
        self.assertEqual(self.bag('project[P.v](P join[P.v=Q.v] Q)'), [(1,)] * 6 + [(2,)] * 2)

    def test_set_versions_return_no_duplicates(self):
        self.assertEqual(self.rows('P union Q'), {(1,), (2,), (3,), (4,)})
        self.assertEqual(self.rows('P intersect Q'), {(1,), (2,)})
        self.assertEqual(self.rows('P minus Q'), {(3,)})
        self.assertEqual(self.rows('P union P'), {(1,), (2,), (3,)})

    def test_bag_versions_count_copies(self):
        self.assertEqual(self.all_version('union'), [(1,)] * 5 + [(2,)] * 3 + [(3,), (4,)])    # m + n
        self.assertEqual(self.all_version('intersect'), [(1,), (1,), (2,)])                    # min(m, n)
        self.assertEqual(self.all_version('minus'), [(1,), (3,)])                              # max(m - n, 0)

    def test_union_and_union_all_differ_on_the_same_inputs(self):
        union = sorted(evaluate(self.parse('P union Q'), MemorySource(self.db)).rows)
        self.assertNotEqual(union, self.all_version('union'))
        self.assertEqual(sorted(set(self.all_version('union'))), union)

    def test_distinct_needs_parentheses_and_is_not_a_relation_name(self):
        with self.assertRaises(ParseError):
            self.parse('distinct P')
        with self.assertRaises(ParseError):
            self.parse('distinct union P')


class QualifiedNameTests(RATestCase):
    files = ('tests/data/company.ra',)

    def test_qualified_names_work_in_every_operator(self):
        self.assertEqual(self.rows("select[Emp.Age > 28 and Emp.DID = 'D1'](Emp)"),
                         {('E1', 'John', 32, 'D1', 'E3'), ('E3', 'Bob', 29, 'D1', 'E3')})
        self.assertEqual(self.schema('project[Emp.Name, Emp.Age](Emp)'), ['Emp.Name', 'Emp.Age'])
        self.assertEqual(self.rows('project[E2.Name](select[E2.Age < 29](rename[E2](Emp)))'), {('Alice',)})
        self.assertEqual(self.rows("project[Emp.Name, Dept.DName](select[Dept.DName = 'HR']"
                                   "(Emp join[Emp.DID = Dept.DID] Dept))"), {('Alice', 'HR')})
        self.assertEqual(self.schema('distinct(project[Dept.DID](Emp times Dept))'), ['Dept.DID'])

    def test_qualifier_must_match_the_relation(self):
        for q in ['select[Dept.Age > 1](Emp)',                 # Age is not in Dept
                  'project[Emp.Name](rename[E2](Emp))',        # after the rename the table is E2
                  'select[Emp.DName = 1](Emp times Dept)']:    # DName belongs to Dept
            with self.subTest(query=q):
                with self.assertRaises(NameErrorRA):
                    self.evaluate(q)

    def test_same_column_name_in_two_tables_is_told_apart_by_qualifier(self):
        q = 'Emp join[Emp.DID = Dept.DID] Dept'
        self.assertEqual(self.bag(f'project[Emp.DID]({q})'), self.bag(f'project[Dept.DID]({q})'))
        with self.assertRaises(NameErrorRA):
            self.evaluate(f'project[DID]({q})')
