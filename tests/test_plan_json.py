"""The plan tree as JSON: every node type goes out and comes back unchanged, and a plan that
does not follow the format is rejected with a message naming the node."""
import json
from tests.base import RATestCase
from source.ast import RelationRef, UnaryOp, BinaryOp, Attr
from source.engine import evaluate, MemorySource
from source.plan_json import to_json, from_json, dumps, loads, LOGICAL_OPS
from source.errors import PlanError

# One query per node type of the tree.
QUERIES = {
    'scan': 'Emp',
    'select': "select[Emp.Age > 28 and not (Emp.Name = 'O''Brien' or Emp.DID != 'D1')](Emp)",
    'project': 'project[Emp.Name, Age](Emp)',
    'rename': 'rename[E2](Emp)',
    'distinct': 'distinct(project[DID](Emp))',
    'join': 'Emp join[Emp.DID = Dept.DID] Dept',
    'times': 'Emp times Dept',
    'union': 'Emp union Emp',
    'intersect': 'Emp intersect Emp',
    'except': 'Emp minus Emp',
    'everything': 'distinct(project[Emp.Name, E2.Name]('
                  'select[Emp.Age >= 29](rename[E2](Emp) join[Emp.MgrID = E2.EID] Emp)))'
                  ' union project[Name, Name2](rename[X](project[Name](Emp)) times rename[Y](project[DName](Dept)))',
}


class RoundTripTests(RATestCase):
    files = ('tests/data/company.ra',)

    def trees(self):
        for name, query in QUERIES.items():
            yield name, self.parse(query)
        scan = RelationRef('Emp')                 # the nodes below have no query syntax
        for op in ('union', 'intersect', 'minus'):
            yield op + ' all', BinaryOp(op, scan, scan, 'all')
        did, age = Attr('Emp', 'DID'), Attr('Emp', 'Age')
        grouped = UnaryOp('group_by', scan, ([did], [('count', None, 'n'), ('avg', age, 'avg_age')]))
        yield 'group_by', grouped
        yield 'group_by without keys', UnaryOp('group_by', scan, ([], [('count', None, 'n')]))
        yield 'sort', UnaryOp('sort', grouped, [(Attr(None, 'n'), 'desc'), (did, 'asc')])
        yield 'limit', UnaryOp('limit', scan, 10)
        yield 'project with a new name', UnaryOp('project', scan, [(Attr('Emp', 'Name'), 'who'), age])

    def test_every_node_type_round_trips(self):
        seen = set()
        for name, tree in self.trees():
            with self.subTest(node=name):
                obj = to_json(tree)
                seen.add(obj['op'])
                text = json.dumps(obj)                      # through real JSON text, not just dicts
                self.assertEqual(from_json(json.loads(text)), tree)
                self.assertEqual(loads(dumps(tree)), tree)
        self.assertEqual(seen, set(LOGICAL_OPS))            # every operator the format knows
        self.assertEqual(len(seen), 16)

    def test_the_format(self):
        self.assertEqual(to_json(self.parse('select[Emp.Age>30](Emp join[Emp.DID=Dept.DID] Dept)')), {
            'op': 'select', 'condition': 'Emp.Age > 30',
            'child': {'op': 'join', 'condition': 'Emp.DID = Dept.DID',
                      'left': {'op': 'scan', 'table': 'Emp'},
                      'right': {'op': 'scan', 'table': 'Dept'}}})
        self.assertEqual(to_json(self.parse('project[Emp.Name, Age](Emp)'))['columns'], ['Emp.Name', 'Age'])
        self.assertEqual(to_json(self.parse('rename[E2](Emp) times Dept')), {
            'op': 'cross', 'left': {'op': 'rename', 'to': 'E2', 'child': {'op': 'scan', 'table': 'Emp'}},
            'right': {'op': 'scan', 'table': 'Dept'}})
        self.assertEqual(to_json(self.parse('Emp minus Emp'))['op'], 'except')
        self.assertEqual(to_json(BinaryOp('union', RelationRef('A'), RelationRef('B'), 'all'))['op'], 'union_all')

    def test_group_sort_and_limit_run(self):
        plan = loads(json.dumps({
            'op': 'limit', 'count': 1,
            'child': {'op': 'sort', 'keys': [['n', 'desc']],
                      'child': {'op': 'group_by', 'keys': ['Emp.DID'],
                                'aggregates': [['count', '*', 'n'], ['max', 'Emp.Age', 'oldest']],
                                'child': {'op': 'scan', 'table': 'Emp'}}}}))
        result = evaluate(plan, MemorySource(self.db))
        self.assertEqual([a.qualified() for a in result.schema], ['Emp.DID', 'n', 'oldest'])
        self.assertEqual(list(result.rows), [('D1', 2, 32)])

    def test_a_plan_read_from_json_runs_to_the_same_result(self):
        for name, query in QUERIES.items():
            if name in ('union', 'everything'):
                continue
            with self.subTest(query=query):
                plan = loads(dumps(self.parse(query)))
                result = evaluate(plan, MemorySource(self.db)).materialize()
                self.assertEqual(sorted(result.rows, key=repr), self.bag(query))


class BadPlanTests(RATestCase):

    def rejected(self, obj):
        with self.assertRaises(PlanError) as cm:
            from_json(obj)
        return str(cm.exception)

    def test_missing_field(self):
        self.assertEqual(self.rejected({'op': 'select', 'child': {'op': 'scan', 'table': 'R'}}),
                         "Node 'select': missing required field 'condition'")

    def test_wrong_type_of_field(self):
        self.assertEqual(self.rejected({'op': 'select', 'condition': 5, 'child': {'op': 'scan', 'table': 'R'}}),
                         "Node 'select': field 'condition' must be a string")
        self.assertIn("field 'child' must be an object",
                      self.rejected({'op': 'distinct', 'child': 'R'}))

    def test_unknown_operator(self):
        self.assertEqual(self.rejected({'op': 'merge_join', 'left': {}, 'right': {}}),
                         "Node 'merge_join': unknown operator 'merge_join'")

    def test_error_inside_a_nested_node_names_that_node(self):
        plan = {'op': 'distinct', 'child': {'op': 'project', 'columns': [], 'child': {'op': 'scan', 'table': 'R'}}}
        self.assertIn("Node 'project'", self.rejected(plan))

    def test_bad_condition_and_bad_column(self):
        self.assertIn("Node 'join': field 'condition' is not a valid condition",
                      self.rejected({'op': 'join', 'condition': 'R.b =', 'left': {'op': 'scan', 'table': 'R'},
                                     'right': {'op': 'scan', 'table': 'S'}}))
        self.assertIn("'R.' is not a column name",
                      self.rejected({'op': 'project', 'columns': ['R.'], 'child': {'op': 'scan', 'table': 'R'}}))

    def test_other_bad_plans(self):
        for obj in [[], 'scan', {}, {'table': 'R'}, {'op': 'scan', 'table': 'R', 'extra': 1},
                    {'op': 'union', 'all': True, 'left': {'op': 'scan', 'table': 'R'},
                     'right': {'op': 'scan', 'table': 'R'}},
                    {'op': 'limit', 'count': -1, 'child': {'op': 'scan', 'table': 'R'}},
                    {'op': 'limit', 'count': True, 'child': {'op': 'scan', 'table': 'R'}},
                    {'op': 'sort', 'keys': [['R.a', 'up']], 'child': {'op': 'scan', 'table': 'R'}},
                    {'op': 'group_by', 'keys': [], 'aggregates': [['median', 'R.a', 'm']],
                     'child': {'op': 'scan', 'table': 'R'}},
                    {'op': 'rename', 'name': 'X', 'child': {'op': 'scan', 'table': 'R'}}]:
            with self.subTest(plan=obj):
                self.rejected(obj)

    def test_malformed_json_text_reports_where(self):
        with self.assertRaises(PlanError) as cm:
            loads('{"op": "scan", "table": }')
        self.assertIn('line 1, column 25', str(cm.exception))
