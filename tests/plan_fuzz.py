"""Random physical plans over the test tables, for checking the engine against the reference.

random_plan(rng) builds a type-correct plan tree: every input of a join is renamed to a fresh
alias, aggregates get explicit output names, and set operators are fed single INT columns.
Not a test module itself; tests/test_exec_fuzz.py uses it.
"""
from tests.execbase import TABLES

SMALL = ['S', 'T', 'L4', 'R3', 'Emp', 'E', 'H', 'V']
MODES = ['sort', 'hash']
JOINS = ['nested_loop_join', 'sort_merge_join', 'hash_join']


class Builder:

    def __init__(self, rng):
        self.rng = rng
        self.names = 0

    def fresh(self, prefix):
        self.names += 1
        return f'{prefix}{self.names}'

    def table(self):
        name = self.rng.choice(SMALL + ['R'] if self.rng.random() < 0.15 else SMALL)
        alias = self.fresh('t')
        columns = [(f'{alias}.{c}', kind) for c, kind, _ in TABLES[name][0]]
        return {'op': 'rename', 'to': alias, 'child': {'op': 'seqscan', 'table': name}}, columns

    def ints(self, columns):
        return [name for name, kind in columns if kind == 'INT']

    def plan(self, depth):
        """-> (plan, [(column name, kind)])"""
        rng = self.rng
        if depth == 0 or rng.random() < 0.2:
            return self.table()
        op = rng.choice(['filter', 'project', 'sort', 'distinct', 'group', 'join', 'join', 'setop', 'limit_sort'])
        if op == 'join':
            return self.join(depth)
        if op == 'setop':
            return self.setop(depth)
        child, columns = self.plan(depth - 1)
        ints = self.ints(columns)
        if op == 'filter' and ints:
            column = rng.choice(ints)
            condition = f"{column} {rng.choice(['<', '>=', '=', '<>'])} {rng.choice([0, 1, 2, 3, 7, 30])}"
            if rng.random() < 0.3:
                condition += f" {rng.choice(['and', 'or'])} not {rng.choice(ints)} = {rng.choice([0, 2])}"
            return {'op': 'filter', 'condition': condition, 'child': child}, columns
        if op == 'project':
            keep = rng.sample(columns, rng.randint(1, len(columns)))
            return {'op': 'project', 'columns': [name for name, _ in keep], 'child': child}, keep
        if op == 'sort':
            keys = rng.sample(columns, rng.randint(1, min(2, len(columns))))
            return {'op': 'sort', 'keys': [[name, rng.choice(['asc', 'desc'])] for name, _ in keys],
                    'child': child}, columns
        if op == 'distinct':
            return {'op': 'distinct', 'mode': rng.choice(MODES), 'child': child}, columns
        if op == 'group':
            keys = rng.sample(columns, rng.randint(0, min(2, len(columns))))
            out = list(keys)
            aggregates = [['count', '*', self.fresh('n')]]
            out.append((aggregates[0][2], 'INT'))
            if keys and ints:                   # with no keys an empty input makes these undefined
                for func in rng.sample(['sum', 'min', 'max', 'avg'], rng.randint(0, 2)):
                    name = self.fresh(func[0])
                    aggregates.append([func, rng.choice(ints), name])
                    out.append((name, 'REAL' if func == 'avg' else 'INT'))
            return {'op': 'group_by', 'mode': rng.choice(MODES), 'keys': [name for name, _ in keys],
                    'aggregates': aggregates, 'child': child}, out
        # limit over a total order, so the rows that survive are determined
        keys = [[name, rng.choice(['asc', 'desc'])] for name, _ in columns]
        distinct = {'op': 'distinct', 'mode': rng.choice(MODES), 'child': child}
        return {'op': 'limit', 'count': rng.choice([0, 1, 5, 40]),
                'child': {'op': 'sort', 'keys': keys, 'child': distinct}}, columns

    def side(self, depth):
        child, columns = self.plan(depth - 1)
        alias = self.fresh('j')
        renamed = [(f"{alias}.{name.rpartition('.')[2]}", kind) for name, kind in columns]
        if len({name for name, _ in renamed}) < len(renamed):          # two columns would collide
            return child, columns
        return {'op': 'rename', 'to': alias, 'child': child}, renamed

    def join(self, depth):
        rng = self.rng
        left, lcols = self.side(depth)
        right, rcols = self.side(depth)
        lints, rints = self.ints(lcols), self.ints(rcols)
        if not lints or not rints or {n for n, _ in lcols} & {n for n, _ in rcols}:
            return left, lcols
        kind = rng.choice(JOINS)
        condition = f'{rng.choice(lints)} = {rng.choice(rints)}'
        if rng.random() < 0.25:
            condition += f' and {rng.choice(lints)} = {rng.choice(rints)}'
        if kind == 'nested_loop_join' and rng.random() < 0.4:
            condition = f"{rng.choice(lints)} {rng.choice(['<', '>', '<>'])} {rng.choice(rints)} and " + condition
        return {'op': kind, 'condition': condition, 'left': left, 'right': right}, lcols + rcols

    def setop(self, depth):
        rng = self.rng
        sides = []
        for _ in range(2):
            child, columns = self.plan(depth - 1)
            ints = self.ints(columns)
            if not ints:
                return child, columns
            column = rng.choice(ints)
            sides.append(({'op': 'project', 'columns': [column], 'child': child}, column))
        op = rng.choice(['union_all', 'union', 'intersect', 'except'])
        return {'op': op, 'left': sides[0][0], 'right': sides[1][0]}, [(sides[0][1], 'INT')]


def random_plan(rng, depth=3):
    return Builder(rng).plan(depth)[0]
