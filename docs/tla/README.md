# Wrev on Modal: review lifecycle and idle repair

This is an executable TLA+ model of the production-shaped Wrev lifecycle and
the proposed idle-repair check. It does not change production.

## How it maps to Wrev

| Model | Deployment |
| --- | --- |
| `Persist`, then `Acknowledge` | Approved GitHub delivery is committed to the single-writer Modal Dict record before HTTP acknowledgement. |
| `Spawn`, `LoseDispatch`, `RecoverDispatch` | The receiver normally submits work to Modal. If dispatch is stranded, a repair tick can submit it later. A healthy queued job remains queued. |
| `Claim`, `StartComment`, `HostRequest`, `ModelReply`, `StoreResult` | A worker takes a slot, posts or reuses a start comment, and runs Wasmtime. Model calls cross the host bridge to vLLM. The worker persists a terminal result against its current claim. |
| `RepairLease` | Repair observes a lost or expired worker and releases the slot. It retries within the attempt budget, then records a terminal failure. |
| `Post`, `ClearLabel`, `RecordPublication` | The publisher posts the outcome, removes the label, and records completion, in that order. Each GitHub effect has a separate step so an interruption can occur between it and the next step. |
| `Probe`, `Wake`, `ResetProbe` | The proposed repair timer reads the Dict directly. A missing/empty record or one with only settled jobs is idle. The probe does not modify the record. |

Source reference: [Wrev deployment, d8fd2ce](https://github.com/r33drichards/wrev-inkling-modal/tree/d8fd2ce01a2c12e205c1962236f83ec1a619a073).

## The race that matters

If a timer observes an empty Dict and a webhook persists before the timer
returns, that timer still skips. The webhook normally spawns the job, and if
it crashes before dispatch the next fair timer observes the pending record
and repairs it. Conversely, if a timer sees work and that work settles
before the wake, one harmless extra wake is allowed. The guarantee concerns
the **snapshot**, not a linearizable guarantee that a coordinator process
never runs after the last job settles.

A stored review result is still work. The label and comment are external
effects; a successful model reply alone cannot make a job settled.

## Run TLC

Get `tla2tools.jar` from the [official TLA+ releases](https://github.com/tlaplus/tlaplus/releases).
From this directory:

```sh
java -cp /path/to/tla2tools.jar tlc2.TLC -config WrevModal.cfg WrevModal
java -cp /path/to/tla2tools.jar tlc2.TLC -config TwoWorkers.cfg WrevModal
# Intentional failures:
java -cp /path/to/tla2tools.jar tlc2.TLC -config AlwaysWake.cfg WrevModal
java -cp /path/to/tla2tools.jar tlc2.TLC -config ResultOnly.cfg WrevModal
```

The positive configs cover two review identities, two allowed review attempts
per identity, and one or two worker slots. One slot forces queueing; two slots
allow simultaneous work. Safety checks cover persist-before-ack, bounded
workers, GPU demand, start-comment and publication order, and no repair wake
requested from an idle snapshot. The temporal checks require that persisted
jobs eventually settle, including terminal failures, and that the GPU
eventually turns off when work drains.

The negative configs are sanity checks:
- `AlwaysWake` requests a repair wake on an initially empty Dict. TLC rejects
  the idle invariant.
- `ResultOnly` stops counting jobs once a result exists. TLC finds a cycle
  where the result is stored, the timer repeatedly skips, the review is never
  posted, and the trigger label never clears.

## Assumptions and limits

- The Dict read sees one coherent snapshot; writes are serialized by one
  coordinator. The model does not prove Modal storage or API semantics.
- The timer keeps recurring and a pending read eventually succeeds. Workers,
  model requests and GitHub effects eventually make progress when runnable,
  under the fairness clauses in the spec. Permanent outages are outside this
  liveness claim. No wall-clock recovery or scale-down bound is claimed.
- Webhook verification, scope checks, and delivery identity are assumed at
  the boundary. Rejected deliveries and duplicate identities are stutters
  in this abstraction. Cryptography, redelivery after tombstone expiry, and
  independent redelivery/dispatch IDs are not modeled.
- Dispatch confirmation races are collapsed into durable, stranded, and
  queued states. Exact lease timers, Modal request routing, and dispatch
  retry budgets are abstracted. The worker attempt epoch explicitly guards
  completion. GitHub comment and label effects are assumed idempotent per job;
  ordering between separate reviews of the same PR is not modeled.
- Wasmtime tools, file contents, model token generation, and the network
  boundary are abstracted, not security-verified. GPU startup/readiness and
  idle shutdown are modeled, not the actual 60-second duration.
- Settled records remain as tombstones in this finite model. Dict expiration
  and lazy pruning are operational concerns rather than state transitions.
  Keep periodic reads comfortably within the Dict inactivity timeout while
  there is work to recover.
