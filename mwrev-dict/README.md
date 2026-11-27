# Modal Dict state backend prototype

This is a shadow prototype, not a production deployment. The live Modal app
still uses Neon. The probe uses an ephemeral Dict, synthetic deliveries, and a
mock CPU reviewer. It needs no production secrets, Postgres, GPU or GitHub app.

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
- The whole record is rewritten on mutation. This intentionally favors atomic
  state transitions over scale for the prototype. A production follow-up
  should measure queue depth, record size, scan fairness and the receiver's
  10-second cold-start deadline. Partition only on a boundary that keeps each
  job and its delivery history in the same atomic write.

## Run

```sh
PYTHONPATH="$PWD/mwrev-candidate:$PWD/mwrev-service" \
  python3 -m unittest discover -s mwrev-dict -v
modal run --env main mwrev-dict/modal_probe.py
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
