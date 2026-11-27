---------------------------- MODULE WrevModal ----------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Jobs, Workers, Attempts, GatePolicy
ASSUME /\ IsFiniteSet(Jobs) /\ Jobs # {}
       /\ Workers \in 1..Cardinality(Jobs)
       /\ Attempts \in Nat \ {0}
       /\ GatePolicy \in {"correct", "always", "result-only"}

VARIABLES job, gpu, gate, sawWork
vars == <<job, gpu, gate, sawWork>>

Phases == {"absent", "durable", "stranded", "queued", "claimed", "guest",
           "inference", "reviewed", "result", "settled"}
Busy == {"claimed", "guest", "inference", "reviewed"}
InitialJob == [phase |-> "absent", ack |-> FALSE, tries |-> 0,
               outcome |-> "none", started |-> FALSE, posted |-> FALSE, cleared |-> FALSE]
Init ==
    /\ job = [j \in Jobs |-> InitialJob]
    /\ gpu = "off"
    /\ gate = "waiting"
    /\ sawWork = FALSE

HasWork == \E j \in Jobs : job[j].phase \notin {"absent", "settled"}
WorkersHeld == {j \in Jobs : job[j].phase \in Busy}
GPURequests == {j \in Jobs : job[j].phase = "inference"}

\* Each j abstracts one authenticated, in-scope delivery/job identity.
\* Duplicates and rejected webhooks create no state change.
Persist(j) ==
    /\ job[j].phase = "absent"
    /\ job' = [job EXCEPT ![j].phase = "durable"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

Acknowledge(j) ==
    /\ job[j].phase # "absent" /\ ~job[j].ack
    /\ job' = [job EXCEPT ![j].ack = TRUE]
    /\ UNCHANGED <<gpu, gate, sawWork>>

IgnoreDelivery == UNCHANGED vars

\* A dispatch crash or an unconfirmed terminal call leaves a durable job
\* for repair. A healthy queued call is not eligible for this transition.
LoseDispatch(j) ==
    /\ job[j].phase = "durable"
    /\ job' = [job EXCEPT ![j].phase = "stranded"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

Spawn(j) ==
    /\ job[j].phase = "durable"
    /\ job' = [job EXCEPT ![j].phase = "queued"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

RecoverDispatch(j) ==
    /\ gate = "awake"
    /\ job[j].phase \in {"durable", "stranded"}
    /\ job' = [job EXCEPT ![j].phase = "queued"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

Claim(j) ==
    /\ job[j].phase = "queued" /\ Cardinality(WorkersHeld) < Workers
    /\ job[j].tries < Attempts
    /\ job' = [job EXCEPT ![j].phase = "claimed", ![j].tries = @ + 1]
    /\ UNCHANGED <<gpu, gate, sawWork>>

\* Create or reuse the start comment. On retry it is the same logical comment.
StartComment(j) ==
    /\ job[j].phase = "claimed"
    /\ job' = [job EXCEPT ![j].phase = "guest", ![j].started = TRUE]
    /\ UNCHANGED <<gpu, gate, sawWork>>

\* Only the trusted host can send a model request. "inference" summarizes
\* potentially many host-mediated model turns. GPU starts on first demand.
HostRequest(j) ==
    /\ job[j].phase = "guest"
    /\ job' = [job EXCEPT ![j].phase = "inference"]
    /\ gpu' = IF gpu = "off" THEN "starting" ELSE gpu
    /\ UNCHANGED <<gate, sawWork>>

GPUReady ==
    /\ gpu = "starting"
    /\ gpu' = "ready"
    /\ UNCHANGED <<job, gate, sawWork>>

GPUIdle ==
    /\ gpu # "off" /\ GPURequests = {}
    /\ gpu' = "off"
    /\ UNCHANGED <<job, gate, sawWork>>

ModelReply(j) ==
    /\ job[j].phase = "inference" /\ gpu = "ready"
    /\ job' = [job EXCEPT ![j].phase = "reviewed"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

\* A completion must present its current claim epoch. TLC enumerates old
\* epochs too; they cannot overwrite newer attempts or terminal results.
StoreResult(j, epoch) ==
    /\ job[j].phase = "reviewed" /\ job[j].tries = epoch
    /\ job' = [job EXCEPT ![j].phase = "result",
                           ![j].outcome = "success"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

Complete(j) == \E epoch \in 1..Attempts : StoreResult(j, epoch)

\* Lease loss is observed by repair. Crashes are allowed but not required;
\* the finite attempt budget ensures they cannot prevent settlement forever.
RepairLease(j) ==
    /\ gate = "awake" /\ job[j].phase \in Busy
    /\ IF job[j].tries < Attempts
       THEN job' = [job EXCEPT ![j].phase = "stranded"]
       ELSE job' = [job EXCEPT ![j].phase = "result",
                                ![j].outcome = "failed"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

\* Nonmutating Dict read. Another writer action can race between inspection
\* and the wake/skip decision. An idle snapshot never wakes repair.
ProbeWork ==
    CASE GatePolicy = "always" -> TRUE
      [] GatePolicy = "result-only" ->
           \E j \in Jobs : job[j].phase \in
             {"durable", "stranded", "queued", "claimed", "guest", "inference", "reviewed"}
      [] OTHER -> HasWork

Probe ==
    /\ gate = "waiting"
    /\ sawWork' = HasWork
    /\ gate' = IF ProbeWork THEN "requested" ELSE "skip"
    /\ UNCHANGED <<job, gpu>>

Wake ==
    /\ gate = "requested"
    /\ gate' = "awake"
    /\ UNCHANGED <<job, gpu, sawWork>>

ResetProbe ==
    /\ gate \in {"skip", "awake"}
    /\ gate' = "waiting"
    /\ UNCHANGED <<job, gpu, sawWork>>

\* GitHub effects are idempotent per job (update the same comment, remove
\* an existing label). Separate steps model a crash between either GitHub
\* effect and recording publication in Dict. A later tick can resume.
Post(j) ==
    /\ gate = "awake" /\ job[j].phase = "result" /\ ~job[j].posted
    /\ job' = [job EXCEPT ![j].posted = TRUE]
    /\ UNCHANGED <<gpu, gate, sawWork>>

ClearLabel(j) ==
    /\ gate = "awake" /\ job[j].phase = "result"
    /\ job[j].posted /\ ~job[j].cleared
    /\ job' = [job EXCEPT ![j].cleared = TRUE]
    /\ UNCHANGED <<gpu, gate, sawWork>>

RecordPublication(j) ==
    /\ gate = "awake" /\ job[j].phase = "result"
    /\ job[j].posted /\ job[j].cleared
    /\ job' = [job EXCEPT ![j].phase = "settled"]
    /\ UNCHANGED <<gpu, gate, sawWork>>

Next ==
    \/ Probe \/ Wake \/ ResetProbe \/ GPUReady \/ GPUIdle \/ IgnoreDelivery
    \/ \E j \in Jobs :
           \/ Persist(j) \/ Acknowledge(j) \/ LoseDispatch(j) \/ Spawn(j)
           \/ RecoverDispatch(j) \/ Claim(j) \/ StartComment(j) \/ HostRequest(j) \/ ModelReply(j)
           \/ Complete(j) \/ RepairLease(j)
           \/ Post(j) \/ ClearLabel(j) \/ RecordPublication(j)

SafetySpec == Init /\ [][Next]_vars

\* Cron continues running; runnable workers, model, and GitHub operations
\* eventually get a turn. The repair gate is intermittent, hence SF there.
Progress(j) ==
    \/ RecoverDispatch(j) \/ Claim(j) \/ StartComment(j)
    \/ HostRequest(j) \/ ModelReply(j) \/ Complete(j)
    \/ Post(j) \/ ClearLabel(j) \/ RecordPublication(j)

Fairness ==
    /\ WF_vars(Probe) /\ WF_vars(Wake) /\ WF_vars(ResetProbe)
    /\ WF_vars(GPUReady) /\ WF_vars(GPUIdle)
    /\ \A j \in Jobs : SF_vars(Progress(j))

Spec == SafetySpec /\ Fairness

TypeOK ==
    /\ job \in [Jobs -> [phase : Phases, ack : BOOLEAN, tries : 0..Attempts,
                          outcome : {"none", "success", "failed"},
                          started : BOOLEAN, posted : BOOLEAN, cleared : BOOLEAN]]
    /\ gpu \in {"off", "starting", "ready"}
    /\ gate \in {"waiting", "requested", "skip", "awake"}
    /\ sawWork \in BOOLEAN

AckIsDurable == \A j \in Jobs : job[j].ack => job[j].phase # "absent"
WorkerLimit == Cardinality(WorkersHeld) <= Workers
DemandPowersGPU == GPURequests # {} => gpu # "off"
StartBeforeInference == \A j \in Jobs : job[j].phase \in {"guest", "inference", "reviewed"} => job[j].started
PublicationOrder ==
    \A j \in Jobs :
       /\ job[j].posted => job[j].outcome # "none"
       /\ job[j].cleared => job[j].posted
       /\ job[j].phase = "settled" => (job[j].posted /\ job[j].cleared)
IdleNeverRequestsWake == gate \in {"requested", "awake"} => sawWork

AcceptedEventuallySettles ==
    \A j \in Jobs : (job[j].phase # "absent") ~> (job[j].phase = "settled")

QuiescentEventuallyGPUOff == (~HasWork) ~> (gpu = "off")
=============================================================================
