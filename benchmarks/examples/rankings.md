# Fixed-pool ranking comparison

**Development fixtures included: 7/7 cases. These are tool tests, not real benefit evidence.**

Fixed-pool ranking and simulated snippet bytes only; no measured repair, reading, time or cost benefit.

Candidate coverage: 6/7. Improvement opportunity: 5 cases.

| Population | Arm | Hit@1 | Hit@3 | MRR |
| --- | --- | ---: | ---: | ---: |
| overall | A | 0.1429 | 0.4286 | 0.3929 |
| overall | B | 0.4286 | 0.8571 | 0.6429 |
| overall | C | 0.4286 | 0.5714 | 0.5714 |
| in_pool | A | 0.1667 | 0.5000 | 0.4583 |
| in_pool | B | 0.5000 | 1.0000 | 0.7500 |
| in_pool | C | 0.5000 | 0.6667 | 0.6667 |

| Case | Fixture | C source/status | Arm | Order | First relevant rank | Simulated bytes |
| --- | --- | --- | --- | --- | ---: | ---: |
| improvement | True | fake/ranked | A | c1, c2, c3, c4 | 4 | 51 |
| improvement | True | fake/ranked | B | c4, c2, c3, c1 | 1 | 21 |
| improvement | True | fake/ranked | C | c4, c2, c3, c1 | 1 | 21 |
| first-root | True | fake/ranked | A | c1, c2, c3, c4 | 1 | 4 |
| first-root | True | fake/ranked | B | c1, c4, c2, c3 | 1 | 4 |
| first-root | True | fake/ranked | C | c1, c4, c3, c2 | 1 | 4 |
| missing-root | True | fake/ranked | A | c1, c2, c3, c4 | miss | 51 |
| missing-root | True | fake/ranked | B | c4, c2, c3, c1 | miss | 51 |
| missing-root | True | fake/ranked | C | c4, c2, c3, c1 | miss | 51 |
| wrong-order | True | fake/ranked | A | c1, c2, c3, c4 | 2 | 16 |
| wrong-order | True | fake/ranked | B | c4, c2, c3, c1 | 2 | 33 |
| wrong-order | True | fake/ranked | C | c1, c3, c4, c2 | 4 | 51 |
| partial-fallback | True | fake/partial | A | c1, c2, c3, c4 | 4 | 51 |
| partial-fallback | True | fake/partial | B | c4, c2, c3, c1 | 1 | 21 |
| partial-fallback | True | fake/partial | C | c2, c4, c3, c1 | 2 | 33 |
| all-fallback | True | fake/fallback | A | c2, c1, c3, c4 | 4 | 51 |
| all-fallback | True | fake/fallback | B | c2, c4, c3, c1 | 2 | 33 |
| all-fallback | True | fake/fallback | C | c2, c1, c3, c4 | 4 | 51 |
| multiple-relevant | True | fake/ranked | A | c1, c2, c3, c4 | 2 | 16 |
| multiple-relevant | True | fake/ranked | B | c4, c2, c3, c1 | 2 | 33 |
| multiple-relevant | True | fake/ranked | C | c3, c2, c4, c1 | 1 | 14 |

Misses read the whole pool in this simulation. Multiple labels mark relevant evidence; reaching one does not prove a complete diagnosis.
