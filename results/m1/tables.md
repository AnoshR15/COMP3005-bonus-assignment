### Records per page

| data | schema | record length L, bytes (mean) | computed floor(4088 / (L + 4)) | measured, mean over full pages | fewest on a page | most on a page | data pages for 20,000 rows |
|---|---|---|---|---|---|---|---|
| all INT | a:INT, b:INT, c:INT | 12 | 255 | 255 | 255 | 255 | 79 |
| short VARCHAR, every value 3 bytes | a:INT, b:VARCHAR(20), c:INT | 13 | 240 | 240 | 240 | 240 | 84 |
| short VARCHAR, values of 2 to 9 bytes | a:INT, b:VARCHAR(20), c:INT | 15.68 | 207 | 207.38 | 204 | 211 | 97 |
| VARCHAR(200), every value 200 bytes | k:INT, pad:VARCHAR(200) | 206 | 19 | 19 | 19 | 19 | 1053 |

### The traces

| trace | what it is | requests | distinct pages |
|---|---|---|---|
| trace 1 | uniform random over 300 pages | 5000 | 300 |
| trace 2 | 80 percent of requests to 60 hot pages of 300 | 5000 | 296 |
| trace 3 | 80-page file scanned ten times, as specified | 800 | 80 |
| trace 4 | working set of 30 pages, a 200-page scan every 300 requests | 5000 | 230 |
| trace 5 | skewed: page i requested in proportion to 1/(i+1), 500 pages | 5000 | 454 |

### Hit rate under both policies

| trace | B | requests | LRU hits | LRU hit rate | LRU evictions | CLOCK hits | CLOCK hit rate | CLOCK evictions |
|---|---|---|---|---|---|---|---|---|
| trace 1 | 4 | 5000 | 58 | 1.2% | 4938 | 57 | 1.1% | 4939 |
| trace 1 | 16 | 5000 | 244 | 4.9% | 4740 | 247 | 4.9% | 4737 |
| trace 1 | 64 | 5000 | 1019 | 20.4% | 3917 | 1026 | 20.5% | 3910 |
| trace 1 | 256 | 5000 | 4104 | 82.1% | 640 | 4105 | 82.1% | 639 |
| trace 2 | 4 | 5000 | 238 | 4.8% | 4758 | 233 | 4.7% | 4763 |
| trace 2 | 16 | 5000 | 867 | 17.3% | 4117 | 863 | 17.3% | 4121 |
| trace 2 | 64 | 5000 | 2998 | 60.0% | 1938 | 2870 | 57.4% | 2066 |
| trace 2 | 256 | 5000 | 4617 | 92.3% | 127 | 4605 | 92.1% | 139 |
| trace 3 | 4 | 800 | 0 | 0.0% | 796 | 0 | 0.0% | 796 |
| trace 3 | 16 | 800 | 0 | 0.0% | 784 | 0 | 0.0% | 784 |
| trace 3 | 64 | 800 | 0 | 0.0% | 736 | 0 | 0.0% | 736 |
| trace 3 | 256 | 800 | 720 | 90.0% | 0 | 720 | 90.0% | 0 |
| trace 4 | 4 | 5000 | 375 | 7.5% | 4621 | 374 | 7.5% | 4622 |
| trace 4 | 16 | 5000 | 1542 | 30.8% | 3442 | 1517 | 30.3% | 3467 |
| trace 4 | 64 | 5000 | 2700 | 54.0% | 2236 | 2700 | 54.0% | 2236 |
| trace 4 | 256 | 5000 | 4770 | 95.4% | 0 | 4770 | 95.4% | 0 |
| trace 5 | 4 | 5000 | 628 | 12.6% | 4368 | 592 | 11.8% | 4404 |
| trace 5 | 16 | 5000 | 1690 | 33.8% | 3294 | 1565 | 31.3% | 3419 |
| trace 5 | 64 | 5000 | 2977 | 59.5% | 1959 | 2906 | 58.1% | 2030 |
| trace 5 | 256 | 5000 | 4210 | 84.2% | 534 | 4176 | 83.5% | 568 |

### Scan of a 500-page table (501 pages in the file: 500 data and 1 directory; the catalog is 1 page)

| B | rows | page reads | hits | misses | evictions | writes | most pages pinned at once |
|---|---|---|---|---|---|---|---|
| 2 | 127500 | 502 | 1 | 502 | 500 | 0 | 2 |
| 8 | 127500 | 502 | 1 | 502 | 494 | 0 | 2 |
| 64 | 127500 | 502 | 1 | 502 | 438 | 0 | 2 |
