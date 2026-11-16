### Estimated against actual, per query (B = 8)

| query | nodes | median q-error | worst q-error | est rows at root | actual rows at root | est cost | actual I/O |
|---|---|---|---|---|---|---|---|
| Q1 | 3 | 1.00 | 1.00 | 57 | 57 | 7 | 7 |
| Q2 | 3 | 1.02 | 1.02 | 182 | 178 | 40 | 40 |
| Q3 | 3 | 1.22 | 1.22 | 44 | 36 | 40 | 40 |
| Q4 | 9 | 1.00 | 1.01 | 978 | 970 | 55 | 55 |
| Q5 | 10 | 1.00 | 2.12 | 55 | 26 | 49 | 49 |
| Q6 | 15 | 1.00 | 1.05 | 297 | 311 | 138 | 160 |
| Q7 | 3 | 1.00 | 1.00 | 5 | 5 | 40 | 40 |
| Q8 | 8 | 1.00 | 1.00 | 6 | 6 | 61 | 53 |
| Q9 | 5 | 1.00 | 1.06 | 34 | 32 | 40 | 40 |
| Q10 | 4 | 1.00 | 1.48 | 196 | 132 | 42 | 42 |
| Q11 | 5 | 1.00 | 19.90 | 10 | 10 | 40 | 40 |
| Q12 | 20 | 1.00 | 1.27 | 1047 | 1290 | 184 | 269 |
| Q13 | 25 | 1.00 | 1.29 | 199 | 155 | 137 | 162 |
| Q14 | 30 | 1.00 | 1.88 | 162 | 86 | 120 | 121 |
| Q15 | 10 | 1.06 | 13.50 | 27 | 2 | 82 | 82 |
| Q16 | 12 | 1.00 | 8.50 | 51 | 6 | 49 | 49 |
| Q17 | 8 | 1.03 | 1.05 | 170 | 165 | 47 | 47 |
| Q18 | 9 | 1.00 | 4.06 | 16 | 65 | 50 | 50 |
| Q19 | 9 | 1.00 | 4.50 | 14 | 63 | 49 | 49 |
| Q20 | 9 | 1.00 | 5.09 | 149 | 567 | 96 | 124 |

### q-error over every node of every plan

| nodes | median | mean | 90th percentile | worst | nodes within 1.5 | nodes above 10 |
|---|---|---|---|---|---|---|
| 200 | 1.00 | 1.54 | 1.88 | 19.90 | 179 | 3 |

### The twelve worst estimates

| q-error | query | node | estimated rows | actual rows |
|---|---|---|---|---|
| 19.9 | Q11 | sort (Orders.total desc) | 199 | 10 |
| 13.5 | Q15 | project (a.oid, b.oid) | 27 | 2 |
| 13.5 | Q15 | nested_loop_join (b.cid = a.cid) | 27 | 2 |
| 8.5 | Q16 | project (c.name, t.n) | 51 | 6 |
| 8.5 | Q16 | nested_loop_join (c.cid = t.cid) | 51 | 6 |
| 5.1 | Q20 | project (o.oid, o.note) | 77 | 392 |
| 5.1 | Q20 | filter (o.odate > 800 and o.shipdate > 800) | 77 | 392 |
| 4.5 | Q19 | project (c.name, o.oid) | 14 | 63 |
| 4.5 | Q19 | project (c.name, c.cid) | 2 | 9 |
| 4.5 | Q19 | nested_loop_join (o.cid = c.cid) | 14 | 63 |
| 4.5 | Q19 | filter (c.city = 'Lima' and c.country = 'Peru') | 2 | 9 |
| 4.1 | Q18 | project (p.pname, i.price) | 16 | 65 |

### Page I/O with and without the optimizer (B = 8)

| query | rows | I/O unoptimized | I/O optimized | ratio | comparisons unoptimized | comparisons optimized |
|---|---|---|---|---|---|---|
| Q1 | 57 | 7 | 7 | 1.00 | 300 | 300 |
| Q2 | 178 | 40 | 40 | 1.00 | 2000 | 2000 |
| Q3 | 36 | 40 | 40 | 1.00 | 2000 | 2000 |
| Q4 | 970 | 87 | 55 | 1.58 | 602000 | 293000 |
| Q5 | 26 | 87 | 49 | 1.78 | 1200000 | 13025 |
| Q6 | 311 | 1343 | 160 | 8.39 | 6603000 | 707510 |
| Q7 | 5 | 128 | 40 | 3.20 | 19898 | 2000 |
| Q8 | 6 | 513 | 53 | 9.68 | 623179 | 602000 |
| Q9 | 32 | 128 | 40 | 3.20 | 23295 | 2823 |
| Q10 | 132 | 42 | 42 | 1.00 | 8480 | 3636 |
| Q11 | 10 | 40 | 40 | 1.00 | 3266 | 3266 |
| Q12 | 1290 | 1225 | 269 | 4.55 | 7503000 | 5410246 |
| Q13 | 155 | 2841 | 162 | 17.54 | 9007343 | 810272 |
| Q14 | 86 | 2296 | 121 | 18.98 | 9227633 | 821250 |
| Q15 | 2 | 314 | 82 | 3.83 | 4015378 | 4770 |
| Q16 | 6 | 215 | 49 | 4.39 | 113295 | 5646 |
| Q17 | 165 | 47 | 47 | 1.00 | 2300 | 2300 |
| Q18 | 65 | 77 | 50 | 1.54 | 602003 | 30200 |
| Q19 | 63 | 87 | 49 | 1.78 | 602000 | 18300 |
| Q20 | 567 | 334 | 124 | 2.69 | 6003000 | 1178000 |
| all |  | 9891 | 1519 | 6.51 | 46163370 | 9912544 |

### Range of the ratio

| smallest | median | largest |
|---|---|---|
| 1.00 | 2.23 | 18.98 |

### Queries whose join algorithms change with B

| query | B = 4 | B = 8 | B = 64 |
|---|---|---|---|
| Q6 | hash + nested_loop | nested_loop + nested_loop | nested_loop + nested_loop |
| Q12 | nested_loop + hash + nested_loop | nested_loop + nested_loop + nested_loop | nested_loop + nested_loop + nested_loop |

### Total page I/O of the workload with one rule turned off (B = 8)

| rule | what it does | I/O with all rules | I/O without this rule | ratio | queries whose I/O changed | largest changes | comparisons, ratio |
|---|---|---|---|---|---|---|---|
| 1 | split conjunctions | 1519 | 2355 | 1.55 | 6 | Q6 +355, Q13 +315, Q14 +137, Q15 +22 | 3.38 |
| 2 | push selects below joins | 1519 | 2476 | 1.63 | 10 | Q6 +355, Q13 +315, Q14 +137, Q20 +108 | 3.35 |
| 3 | cross + select = join | 1519 | 1519 | 1.00 | 0 | - | 1.00 |
| 4 | push selects through rename, project, union | 1519 | 1519 | 1.00 | 0 | - | 1.00 |
| 5 | push selects below group_by | 1519 | 1519 | 1.00 | 0 | - | 1.00 |
| 6 | push projections down | 1519 | 2640 | 1.74 | 9 | Q12 +658, Q13 +177, Q14 +149, Q8 +114 | 0.42 |
| 7 | tidy up | 1519 | 1519 | 1.00 | 0 | - | 1.00 |

### Correlated predicates: the plan chosen against the plan chosen when told the true row count

| predicates | B | estimated rows | actual rows | actual / estimated | I/O of the chosen plan | I/O of the informed plan | ratio |
|---|---|---|---|---|---|---|---|
| odate > 500 AND shipdate > 500, three tables | 7 | 494 | 992 | 2.0 | 610 | 331 | 1.84 |
| odate > 500 AND shipdate > 500, three tables | 6 | 494 | 992 | 2.0 | 616 | 436 | 1.41 |
| odate > 500 AND shipdate > 500, three tables | 8 | 494 | 992 | 2.0 | 370 | 315 | 1.17 |
| odate > 500 AND shipdate > 500, four tables | 16 | 494 | 992 | 2.0 | 386 | 346 | 1.12 |
| odate > 500 AND shipdate > 500, three tables | 12 | 494 | 992 | 2.0 | 246 | 232 | 1.06 |
| odate > 550 AND shipdate > 550, three tables | 12 | 402 | 892 | 2.2 | 235 | 223 | 1.05 |
| odate > 500 AND shipdate > 500, three tables | 10 | 494 | 992 | 2.0 | 277 | 264 | 1.05 |
| odate > 550 AND shipdate > 550, three tables | 10 | 402 | 892 | 2.2 | 265 | 253 | 1.05 |
| odate > 500 AND shipdate > 500, three tables | 16 | 494 | 992 | 2.0 | 199 | 199 | 1.00 |
| odate > 550 AND shipdate > 550, three tables | 16 | 402 | 892 | 2.2 | 191 | 192 | 0.99 |

### The independence assumption on the two correlated pairs

| predicates | rows passing the first | rows passing the second | estimated rows passing both | actual rows passing both | q-error |
|---|---|---|---|---|---|
| odate > 800 AND shipdate > 800 | 392 | 398 | 77 | 392 | 5.1 |
| city = 'Lima' AND country = 'Peru' | 9 | 49 | 2 | 9 | 4.5 |
| odate < 100 AND shipdate > 900 | 198 | 181 | 19 | 0 | 19.0 |
