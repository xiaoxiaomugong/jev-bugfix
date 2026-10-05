# Run evidence summary

DEVELOPMENT FIXTURE — synthetic data; no real benefit evidence.

Real runs: 0. Independent tasks: 3.

Benefit status: `insufficient_evidence`. fewer than six complete comparable real tasks

| Arm | Runs | Successes | Success rate | Actual total cost | Known subtotal | Cost/success |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 3 | 3 | 1.0 | unavailable | 0 | unavailable |
| C | 3 | 2 | 0.666667 | unavailable | 0 | unavailable |

Deltas are C minus A; comparisons use both successful, protocol-compliant runs.

| Pair | Task | Seconds delta | Observed bytes delta | Reading reduction | Actual cost delta | Exclusions |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| p1 | dev-case1 | 0.0 | -70 | 0.583333 | unavailable | missing total_real_cost |
| p2 | dev-case2 | unavailable | unavailable | unavailable | unavailable | not both successful; retained in arm totals |
| p3 | dev-case3 | 0.0 | unavailable | unavailable | unavailable | missing total_real_cost; incomplete or incomparable source capture |

Evidence index (all runs, including failures):

- `A1`: success; elapsed 10.0 s; exposed 120 bytes (complete); evidence benchmarks/examples/synthetic.log
- `C1`: success; elapsed 10.0 s; exposed 50 bytes (complete); evidence benchmarks/examples/synthetic.log
- `A2`: success; elapsed 10.0 s; exposed 120 bytes (complete); evidence benchmarks/examples/synthetic.log
- `C2`: failure; elapsed 10.0 s; exposed 50 bytes (complete); evidence benchmarks/examples/synthetic.log
- `A3`: success; elapsed 10.0 s; exposed 120 bytes (complete); evidence benchmarks/examples/synthetic.log
- `C3`: success; elapsed 10.0 s; exposed 50 bytes (lower_bound); evidence benchmarks/examples/synthetic.log

Limitations:

- Normalized sources/completeness are declarations requiring evaluator audit.
- Development fixtures do not establish real benefits.
- Reading bytes include repeated exposure; unique lines use path plus file version.
- Estimated prices, HTTP upper bounds and subscription percentages are not bills.
