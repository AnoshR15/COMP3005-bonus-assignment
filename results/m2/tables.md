### Cost against formula

| join | M | N | B | reads: formula | reads: measured | writes: formula | writes: measured | I/O: formula | I/O: measured | comparisons | peak frames |
|---|---|---|---|---|---|---|---|---|---|---|---|
| block nested loop | 20 | 10 | 4 | 120 | 120 | 0 | 0 | 120 | 120 | 72200 | 4 |
| sort-merge | 20 | 10 | 4 | 110 | 101 | 80 | 80 | 190 | 181 | 4902 | 4 |
| hash | 20 | 10 | 4 | 60 | 115 | 30 | 73 | 90 | 188 | 377 | 4 |
| block nested loop | 20 | 10 | 16 | 40 | 40 | 0 | 0 | 40 | 40 | 72200 | 16 |
| sort-merge | 20 | 10 | 16 | 80 | 71 | 50 | 50 | 130 | 121 | 4819 | 16 |
| hash | 20 | 10 | 16 | 60 | 74 | 30 | 44 | 90 | 118 | 396 | 16 |
| block nested loop | 20 | 10 | 64 | 30 | 30 | 0 | 0 | 30 | 30 | 72200 | 63 |
| sort-merge | 20 | 10 | 64 | 60 | 51 | 30 | 30 | 90 | 81 | 4715 | 64 |
| hash | 20 | 10 | 64 | 60 | 146 | 30 | 121 | 90 | 267 | 345 | 64 |
| block nested loop | 20 | 10 | 256 | 30 | 30 | 0 | 0 | 30 | 30 | 72200 | 255 |
| sort-merge | 20 | 10 | 256 | 60 | 51 | 30 | 30 | 90 | 81 | 4715 | 256 |
| hash | 20 | 10 | 256 | 60 | 298 | 30 | 334 | 90 | 632 | 290 | 256 |
| block nested loop | 60 | 40 | 4 | 1260 | 1260 | 0 | 0 | 1260 | 1260 | 866400 | 4 |
| sort-merge | 60 | 40 | 4 | 500 | 473 | 400 | 392 | 900 | 865 | 20512 | 4 |
| hash | 60 | 40 | 4 | 200 | 495 | 100 | 340 | 300 | 835 | 3821 | 4 |
| block nested loop | 60 | 40 | 16 | 260 | 260 | 0 | 0 | 260 | 260 | 866400 | 16 |
| sort-merge | 60 | 40 | 16 | 300 | 281 | 200 | 200 | 500 | 481 | 18910 | 16 |
| hash | 60 | 40 | 16 | 200 | 214 | 100 | 114 | 300 | 328 | 1487 | 16 |
| block nested loop | 60 | 40 | 64 | 100 | 100 | 0 | 0 | 100 | 100 | 866400 | 63 |
| sort-merge | 60 | 40 | 64 | 200 | 181 | 100 | 100 | 300 | 281 | 18638 | 64 |
| hash | 60 | 40 | 64 | 200 | 249 | 100 | 149 | 300 | 398 | 1434 | 64 |
| block nested loop | 60 | 40 | 256 | 100 | 100 | 0 | 0 | 100 | 100 | 866400 | 255 |
| sort-merge | 60 | 40 | 256 | 200 | 181 | 100 | 100 | 300 | 281 | 18638 | 256 |
| hash | 60 | 40 | 256 | 200 | 584 | 100 | 491 | 300 | 1075 | 1374 | 256 |
| block nested loop | 120 | 80 | 4 | 4920 | 4920 | 0 | 0 | 4920 | 4920 | 3465600 | 4 |
| sort-merge | 120 | 80 | 4 | 1120 | 1049 | 920 | 888 | 2040 | 1937 | 48466 | 4 |
| hash | 120 | 80 | 4 | 400 | 1164 | 200 | 640 | 600 | 1804 | 130142 | 4 |
| block nested loop | 120 | 80 | 16 | 840 | 840 | 0 | 0 | 840 | 840 | 3465600 | 16 |
| sort-merge | 120 | 80 | 16 | 600 | 561 | 400 | 400 | 1000 | 961 | 44134 | 16 |
| hash | 120 | 80 | 16 | 400 | 414 | 200 | 214 | 600 | 628 | 2990 | 16 |
| block nested loop | 120 | 80 | 64 | 280 | 280 | 0 | 0 | 280 | 280 | 3465600 | 64 |
| sort-merge | 120 | 80 | 64 | 600 | 561 | 400 | 400 | 1000 | 961 | 42977 | 64 |
| hash | 120 | 80 | 64 | 400 | 462 | 200 | 262 | 600 | 724 | 3086 | 64 |
| block nested loop | 120 | 80 | 256 | 200 | 200 | 0 | 0 | 200 | 200 | 3465600 | 255 |
| sort-merge | 120 | 80 | 256 | 400 | 361 | 200 | 200 | 600 | 561 | 42687 | 256 |
| hash | 120 | 80 | 256 | 400 | 708 | 200 | 509 | 600 | 1217 | 2872 | 256 |

### Sort passes

| N pages | B |  | runs: formula | runs: measured | passes: formula | passes: measured | reads: N x passes | reads: measured | writes: N x (passes - 1) | writes: measured |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | 3 |  | 3 | 3 | 3 | 3 | 24 | 22 | 16 | 14 |
| 8 | 4 |  | 2 | 2 | 2 | 2 | 16 | 16 | 8 | 8 |
| 30 | 4 |  | 8 | 8 | 3 | 3 | 90 | 90 | 60 | 60 |
| 80 | 4 | 20 x B | 20 | 20 | 4 | 4 | 320 | 312 | 240 | 232 |
| 120 | 8 |  | 15 | 15 | 3 | 3 | 360 | 352 | 240 | 232 |
| 160 | 8 | 20 x B | 20 | 20 | 3 | 3 | 480 | 480 | 320 | 320 |
| 120 | 16 |  | 8 | 8 | 2 | 2 | 240 | 240 | 120 | 120 |
| 320 | 16 | 20 x B | 20 | 20 | 3 | 3 | 960 | 960 | 640 | 640 |
| 60 | 64 |  | 1 | 1 | 1 | 1 | 60 | 60 | 0 | 0 |
| 320 | 64 |  | 5 | 5 | 2 | 2 | 640 | 640 | 320 | 320 |

### Crossover, relations fixed (M = 120, N = 80), B varies

| B | block nested loop | sort-merge | hash | cheapest |
|---|---|---|---|---|
| 4 | 4920 | 1937 | 1804 | hash |
| 5 | 3320 | 1601 | 1664 | sort-merge |
| 6 | 2520 | 1361 | 1276 | hash |
| 8 | 1720 | 1345 | 1166 | hash |
| 10 | 1320 | 1201 | 1258 | sort-merge |
| 12 | 1080 | 961 | 900 | hash |
| 16 | 840 | 961 | 628 | hash |
| 24 | 600 | 961 | 642 | block nested loop |
| 32 | 440 | 961 | 658 | block nested loop |
| 48 | 360 | 961 | 690 | block nested loop |
| 64 | 280 | 961 | 724 | block nested loop |
| 96 | 280 | 801 | 772 | block nested loop |
| 122 | 200 | 561 | 798 | block nested loop |
| 128 | 200 | 561 | 800 | block nested loop |
| 192 | 200 | 561 | 972 | block nested loop |
| 256 | 200 | 561 | 1217 | block nested loop |

### Crossover, B fixed at 16, relation size varies (M = N)

| M = N pages | block nested loop | sort-merge | hash | cheapest |
|---|---|---|---|---|
| 10 | 20 | 60 | 84 | block nested loop |
| 20 | 60 | 200 | 152 | block nested loop |
| 40 | 160 | 400 | 264 | block nested loop |
| 80 | 560 | 800 | 508 | hash |
| 160 | 2080 | 1600 | 988 | hash |
| 240 | 4560 | 2400 | 2992 | sort-merge |

### Hash join, uniform against skewed keys (M = N = 60 pages)

| keys | B | partitions | table frames | largest left partition, pages | partitioned again | nested loops | reads | writes | I/O | comparisons | rows out | seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| uniform | 4 | 3 | 3 | 22 | 12 | 1 | 578 | 402 | 980 | 5468 | 1140 | 1.3 |
| skewed | 4 | 3 | 3 | 36 | 12 | 1 | 886 | 402 | 1288 | 228784 | 208620 | 1.5 |
| uniform | 16 | 15 | 15 | 6 | 0 | 0 | 256 | 136 | 392 | 2228 | 1140 | 0.5 |
| skewed | 16 | 15 | 15 | 27 | 2 | 1 | 460 | 268 | 728 | 209280 | 208620 | 1.1 |
| uniform | 64 | 63 | 63 | 2 | 0 | 0 | 288 | 168 | 456 | 2144 | 1140 | 1.9 |
| skewed | 64 | 63 | 63 | 25 | 0 | 0 | 296 | 176 | 472 | 209284 | 208620 | 2.1 |

### Partition sizes in pages at B = 16

| keys | left partitions | right partitions |
|---|---|---|
| uniform | 4 6 4 4 5 5 4 5 4 5 4 4 5 5 4 | 4 6 4 4 5 5 4 5 4 5 4 4 5 5 4 |
| skewed | 3 3 3 2 3 27 3 3 3 3 3 3 3 3 2 | 3 3 3 2 3 27 3 3 3 3 3 3 3 3 2 |

### What the overflow handling did, skewed keys, B = 16

| level | left pages | right pages | action |
|---|---|---|---|
| 1 | 27 | 27 | partitioned again |
| 2 | 25 | 25 | partitioned again |
| 3 | 24 | 24 | nested loop |

### The three joins on the skewed data at B = 16

| join | reads | writes | comparisons | rows out | seconds |
|---|---|---|---|---|---|
| block nested loop | 360 | 0 | 1299600 | 208620 | 0.2 |
| sort-merge | 11735 | 240 | 228763 | 208620 | 0.6 |
| hash | 460 | 268 | 209280 | 208620 | 1.2 |

### Plans at B = 4

| plan | B | most frames in use at once | rows out | reads | writes | runs | passes |
|---|---|---|---|---|---|---|---|
| sort of A80 (20 x B pages) | 4 | 4 | 1520 | 312 | 232 | 20 | 4 |
| sort over hash join over nested loop | 4 | 4 | 380 | 2473 | 559 | 64 | 5 |
| distinct (hash) of a sort-merge join | 4 | 4 | 1140 | 1386 | 1266 |  |  |
