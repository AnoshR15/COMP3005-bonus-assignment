"""Evaluation: required cases 14 and 18 to 25, the name, schema and type error categories, the
counters, tuple equality, and the defects fixed during review (DESIGN_LOG.md, session 2)."""
from tests.base import RATestCase
from source.operators import Counters, tuple_equal
from source.errors import NameErrorRA, SchemaError, TypeErrorRA

EXAMPLE = 'data/example.ra'              # the Section 4.1 example: Employees and Departments
COMPANY = 'tests/data/company.ra'        # Emp with a MgrID column, and Dept


class SelectProjectTests(RATestCase):
    files = (EXAMPLE,)

    def test_three_levels_of_nesting(self):
        """Case 14."""
        q = "project[Name](select[Age>30](select[DID='D1'](Employees)))"
        self.assertEqual(self.rows(q), {('John',)})
        self.assertEqual(self.schema(q), ['Employees.Name'])

    def test_number_compared_with_a_string(self):
        """Case 22."""
        with self.assertRaises(TypeErrorRA):
            self.evaluate("select[Age>'30'](Employees)")

    def test_projection_keeps_duplicates_and_distinct_removes_them(self):
        """Case 23, changed by the move from sets to bags. Projection used to remove duplicates
        itself. It now keeps them, and distinct is the operator that removes them."""
        self.assertEqual(self.bag('project[DID](Employees)'), [('D1',), ('D1',), ('D2',)])
        self.assertEqual(self.rows('distinct(project[DID](Employees))'), {('D1',), ('D2',)})

    def test_duplicate_attribute_in_a_projection(self):
        """Case 24. GRAMMAR.md section 7: an attribute may appear once in a projection list."""
        with self.assertRaises(SchemaError):
            self.evaluate('project[Name, Name](Employees)')

    def test_empty_result_keeps_its_schema(self):
        """Case 25."""
        q = 'select[Age>100](Employees)'
        self.assertEqual(self.rows(q), set())
        self.assertEqual(self.schema(q), ['Employees.EID', 'Employees.Name', 'Employees.Age', 'Employees.DID'])

    def test_type_errors(self):
        for q in ['select[Name=3](Employees)', 'select[Name=Age](Employees)']:
            with self.subTest(query=q):
                with self.assertRaises(TypeErrorRA):
                    self.evaluate(q)

    # The next three were accepted silently until conditions were checked against the schema
    # before any row is examined (DESIGN_LOG.md). A condition on an empty input, or on the right
    # of an 'and' that short-circuits, is still checked.

    def test_unknown_attribute_on_an_empty_input(self):
        with self.assertRaises(NameErrorRA):
            self.evaluate('select[Nope=1](select[Age>1000](Employees))')

    def test_type_error_behind_a_short_circuit_and(self):
        with self.assertRaises(TypeErrorRA):
            self.evaluate('select[Age>1000 and Name>5](Employees)')

    def test_name_error_behind_a_short_circuit_and(self):
        with self.assertRaises(NameErrorRA):
            self.evaluate('select[Age>1000 and Nope=1](Employees)')


class ComparisonTests(RATestCase):
    files = ('tests/data/pairs.ra',)

    def test_attribute_compared_with_attribute(self):
        """Case 18: A=B compares two columns, A='B' compares column A with the string B."""
        self.assertEqual(self.rows('select[A=B](R)'), {('x', 'x'), ('B', 'B')})
        self.assertEqual(self.rows("select[A='B'](R)"), {('B', 'B'), ('B', 'q')})


class JoinTests(RATestCase):
    files = (COMPANY,)

    def test_theta_join_keeps_both_did_columns(self):
        """Case 19."""
        q = 'Emp join[Emp.DID=Dept.DID] Dept'
        self.assertEqual(self.schema(q), ['Emp.EID', 'Emp.Name', 'Emp.Age', 'Emp.DID', 'Emp.MgrID',
                                          'Dept.DID', 'Dept.DName'])
        self.assertEqual(len(self.rows(q)), 3)
        # Changed by the move to bags: one row per joined pair, so D1 appears twice.
        self.assertEqual(self.bag('project[Dept.DID](' + q + ')'), [('D1',), ('D1',), ('D2',)])
        self.assertEqual(self.rows('distinct(project[Dept.DID](' + q + '))'), {('D1',), ('D2',)})

    def test_self_join_needs_a_rename(self):
        """Case 20."""
        q = 'project[Emp.Name, E2.Name](rename[E2](Emp) join[Emp.MgrID=E2.EID] Emp)'
        self.assertEqual(self.rows(q), {('John', 'Bob'), ('Alice', 'John'), ('Bob', 'Bob')})
        with self.assertRaises(SchemaError):      # without the rename both sides are Emp.*
            self.evaluate('Emp join[Emp.MgrID=Emp.EID] Emp')

    def test_join_is_times_then_select_without_storing_the_product(self):
        via_join = self.evaluate('Emp join[Emp.DID=Dept.DID] Dept')
        via_product = self.evaluate('select[Emp.DID=Dept.DID](Emp times Dept)')
        self.assertEqual(via_join.rows, via_product.rows)
        self.assertEqual([a.qualified() for a in via_join.schema],
                         [a.qualified() for a in via_product.schema])

    def test_counters_count_every_pair_and_every_tuple(self):
        c = Counters()
        self.evaluate('select[Age>30](Emp join[Emp.DID=Dept.DID] Dept)', c)
        self.assertEqual(c.join_compared, 3 * 2)     # every pair, matching or not
        self.assertEqual(c.select_examined, 3)       # every tuple of the join result

    def test_name_errors(self):
        with self.assertRaises(NameErrorRA):
            self.evaluate('Missing')
        with self.assertRaises(NameErrorRA):
            self.evaluate('select[Salary>1](Emp)')
        with self.assertRaises(NameErrorRA):      # DID is in both Emp and Dept, so it must be qualified
            self.evaluate('select[DID=DID](Emp times Dept)')

    def test_schema_errors(self):
        with self.assertRaises(SchemaError):      # Emp.EID would appear twice
            self.evaluate('Emp times Emp')
        with self.assertRaises(SchemaError):      # Emp.DID and Dept.DID would both become X.DID
            self.evaluate('rename[X](Emp join[Emp.DID=Dept.DID] Dept)')


class SetOperatorTests(RATestCase):
    files = (COMPANY, 'tests/data/sets.ra')

    def test_union_of_different_schemas(self):
        """Case 21."""
        with self.assertRaises(SchemaError):
            self.evaluate('Emp union Dept')

    def test_same_attribute_name_but_different_types(self):
        with self.assertRaises(SchemaError):
            self.evaluate('A union Names')


class TupleEqualityTests(RATestCase):

    def test_definition(self):
        self.assertTrue(tuple_equal((1, 'a'), (1.0, 'a')))       # numbers compare numerically
        self.assertFalse(tuple_equal(('a',), ('A',)))            # strings compare exactly
        self.assertFalse(tuple_equal((1,), (1, 1)))              # arity must match
