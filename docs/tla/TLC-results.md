# TLC verification

Checked locally with TLC 2.19 (TLA+ tools release v1.7.4).

| Config | Generated | Distinct | Result |
| --- | ---: | ---: | --- |
| WrevModal.cfg | 135,829 | 28,046 | Passed every invariant and both temporal properties |
| TwoWorkers.cfg | 153,425 | 30,682 | Passed every invariant and both temporal properties |
| AlwaysWake.cfg | 2 | 2 | Expected invariant failure |
| ResultOnly.cfg | 1,577 | 430 | Expected liveness failure |

The always-wake trace starts from an empty Dict. The first probe requests a
wake even though its own snapshot found no work.

The result-only trace reaches a successful stored result for an acknowledged
job. Its start comment already exists, the final result has not been posted,
and the trigger label has not been cleared. TLC finds an infinite cycle
between read and skip, so that job never settles even though the timer keeps
getting scheduled. Skipping all terminal model results was too aggressive.

A passing finite model is not a proof over the production implementation.
See the README for the exact scope, abstracted failure modes, and fairness
assumptions.
