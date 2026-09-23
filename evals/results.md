# Eval results

Prompt versions: extract `extract/v2`, mapping `mapping/v1`. Offline from `data/llm_cache`.

33 claims over 27 changes.

| Metric | Value | Detail |
| --- | --- | --- |
| Materiality accuracy | 15/16 (94%) | CH-5 expected False, got True |
| Draft/final accuracy | 2/2 (100%) | v2: draft (expected draft; split draft 16); v3: final (expected final; split draft 2, final 15) |
| Extraction precision | 82% | 14 of 17 predicted pairs on gold-judged changes are in gold |
| Extraction recall | 88% | 14 of 16 gold pairs found |
| Citation fidelity | 32 verified, 0 near, 1 rejected | rejection rate 3%; reasons {'quote_outside_changed_span': 1} |
| Mapping precision | 83% | 1 link(s) outside gold and its allowances |
| Mapping recall | 100% | 5 of 5 gold links found |
| Impact coverage | 13/13 (100%) | no misses; 3 two-hop impacts in gold |
| Impact leakage | 0 | nodes reached that gold excludes |

## Escalation at each threshold

| Threshold | Owner queue | Expert queue |
| --- | --- | --- |
| 0.5 | 26 | 7 |
| 0.7 | 26 | 7 |
| 0.9 | 26 | 7 |

Link confidences range 0.95 to 0.98 over 8 links. The counts are identical at every threshold because no link falls below the lowest one: the escalations counted here come from the other rules, not from confidence.
