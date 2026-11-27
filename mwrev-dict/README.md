# Modal Dict state backend prototype

The storage adapter supports an isolated probe and a staged production cutover.
The probe uses an ephemeral Dict, synthetic deliveries, and mock CPU reviewers.
It needs no production secrets, Postgres, GPU or GitHub app.

## Design

- One non-concurrent coordinator serializes state transitions and persists a
  versioned JSON record in Modal Dict. Saving the delivery and the job in one
  write eliminates any gap between acknowledging the job and deduplicating its
  delivery. There is no transaction across Dict keys and no read/modify/write
  from parallel worker containers.
- Jobs keep the existing Wrev worker interface. The receiver persists intent
  before acknowledging or spawning. Dispatch uses Modal Function `.spawn()`;
  it does not require a Modal Queue. A lost spawn acknowledgement is recoverable
  and a duplicate invocation cannot acquire a second live worker lease.
- A lease token fences late writes from expired workers. Confirmed Modal calls
  are not resubmitted just because they have been queued a long time. Recovery
  may requeue when an outer status check records that the exact confirmed call
  ended without claiming. Expired worker leases are recoverable separately.
- Persist results and the start comment ID before publication. The existing
  app-owned GitHub marker is the idempotency key for external comment retries;
  the result stays pending until publication succeeds. After success, leave
  the detailed history on GitHub and keep a seven-day deduplication record.
  The Dict entry itself can expire after seven days without reads or writes.
- The scheduled CPU repair function reads one Dict snapshot before contacting
  the coordinator. If the snapshot has no unsettled jobs, the function skips
  Dict repair without waking the writer. Postgres repair is unchanged in
  bridge mode. A result with pending GitHub effects still counts as work.
  A webhook racing an idle read persists and dispatches as usual, and the
  next repair tick can recover a lost dispatch. Expired settled records are
  also pruned on webhook persistence, so cleanup does not require an idle
  writer wake. The periodic reads keep the shared Dict key alive; this is
  distinct from expiring individual review tombstones. See the [TLA+ model](../docs/tla/README.md).
- The whole record is rewritten on mutation. This intentionally favors atomic
  state transitions over scale for the prototype. A production follow-up
  should measure queue depth, record size, scan fairness and the receiver's
  10-second cold-start deadline. Partition only on a boundary that keeps each
  job and its delivery history in the same atomic write.

## Run

```sh
PYTHONPATH="$PWD/mwrev-candidate:$PWD/mwrev-service" \
  python3 -m unittest discover -s mwrev-dict -v
modal run --env main mwrev-dict/modal_probe.py::verify
modal run --env main mwrev-dict/modal_probe.py::signed_queue
```

## Gates before production

1. Verify Modal call status handling for queued, terminal, and missing calls.
   Do not infer a loss from scheduling age alone.
2. Prove single-coordinator and single-publisher ownership through restart and
   deploy overlap; a per-revision container cap is not a cross-deploy fence.
3. Replay a signed webhook through an isolated receiver with four workers.
   Check latency, redelivery, label changes during a review, and lost GitHub
   acknowledgements using a fixture PR before touching account-wide traffic.
4. Drain old jobs, preserve existing Neon data, and establish a rollback path
   before any cutover. Do not let two stores consume the same live webhook.

## Production cutover and rollback

The CPU app supports an explicit deployment mode in `MWREV_DEPLOY_BACKEND`:
`postgres` (legacy only), `bridge` (write new jobs to Dict and finish older Neon
jobs), or `dict` (default; no Neon credential or operations).

Keep the private `mwrev-state` app independent from `mwrev`. The production
worker app, including overlapping worker releases, can only write via this
same coordinator. Do not run a second coordinator against the same named Dict.
For future coordinator upgrades, quiesce callers, stop the old coordinator
and verify it is gone before starting a replacement (do not roll it).

1. Create the named durable Dict `mwrev-review-state-v1` in the production
   environment and deploy only `mwrev-dict/coordinator_app.py`.
2. Deploy the CPU app with `MWREV_DEPLOY_BACKEND=bridge` and
   `--strategy recreate`. New jobs go to Dict. Already accepted Neon jobs and
   their delivery retries retain their original worker and publication state.
   Check both backends for unfinished jobs.
3. Invoke the deployed `seed_legacy` function. It refuses to copy while any
   Neon job awaits review/publication and copies only recent delivery records
   needed for deduplication (not findings). Keep Neon intact.
4. Deploy the CPU app with `MWREV_DEPLOY_BACKEND=dict` and
   `--strategy recreate`. Verify the signed receiver and completion path.
   The GPU and Railway apps do not change.
5. To restore the bridge, redeploy the same revision in bridge mode. **Never
   point new work back to Neon alone while Dict jobs are active.** A full
   reverse cutover requires draining Dict and preserving its delivery history.
