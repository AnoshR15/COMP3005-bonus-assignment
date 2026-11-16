"""The cost model: estimated page I/O, reads plus writes. CPU is ignored.

M and N are the estimated page counts of the left (outer) and right (inner) input, B the frames.

    seqscan              M
    filter, project,     0     pipelined
    rename, limit
    sort                 2 * M * (passes - 1) + M,   passes = 1 + ceil(log(ceil(M / B)) / log(B - 1))
    nested_loop_join     M + ceil(M / (B - 2)) * N
    sort_merge_join      sort(M) + sort(N) + M + N   + (M + N)       see below
    hash_join            3 * (M + N)                 + 2 * (M + N) per extra partitioning level
    distinct, group_by   sort mode: the sort.  hash mode: M if the groups fit in memory,
                         otherwise M + 4 * M * (the share of the groups that do not fit)

Every formula includes reading its inputs once: that is the M in the sort, the M and the N in
the joins. planner.py says what follows from that when the formulas are added up over a plan.

Two formulas differ from the specification's table, both to agree with what the Module 2
executor does, which REPORT_M2.md measured:

  sort_merge_join  The executor writes each sorted input out as one run and then reads it for
                   the merge, because rewinding over duplicate keys needs the run on disk. The
                   specification's sort() stops before that write. So M + N is added.
  hash_join        3 (M + N) holds while a partition of the build input fits in memory, that is
                   while M <= (B - 1)^2. Beyond that every partition is partitioned again: one
                   more write and read of both inputs for each extra level.

The block nested loop and the sort are the specification's formulas unchanged, and both matched
the executor exactly in REPORT_M2.md.
"""
import math


def sort_passes(m, b):
    """1 + ceil(log(ceil(M / B)) / log(B - 1)), with integers."""
    runs, passes = math.ceil(m / b), 1
    while runs > 1:
        runs = math.ceil(runs / (b - 1))
        passes += 1
    return passes


def sort_cost(m, b):
    return 2 * m * (sort_passes(m, b) - 1) + m


def nested_loop_cost(m, n, b):
    return m + nested_loop_blocks(m, b) * n


def nested_loop_blocks(m, b):
    return math.ceil(m / (b - 2))


def sort_merge_cost(m, n, b):
    return sort_cost(m, b) + sort_cost(n, b) + (m + n) + (m + n)


def hash_levels(m, b):
    """Levels of partitioning the build input of M pages needs before a partition fits in the
    B - 1 frames of the table: 1 when M <= (B - 1)^2."""
    levels, size = 1, m / (b - 1)
    while size > b - 1 and levels < 3:
        size /= (b - 1)
        levels += 1
    return levels


def hash_cost(m, n, b):
    return 3 * (m + n) + 2 * (m + n) * (hash_levels(m, b) - 1)


def hash_group_cost(m, group_pages, b):
    """group_by or distinct in hash mode: no I/O of its own while the table of groups fits in
    the B - 2 frames it gets; otherwise the rows of the groups that do not fit are written and
    read twice (once to a spill file, once to partitions)."""
    table = b - 2
    if group_pages <= table:
        return m
    return m + 4 * m * (1 - table / group_pages)
