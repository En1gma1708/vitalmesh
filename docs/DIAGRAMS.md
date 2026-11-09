# VitalMesh diagrams

Five pictures of the system as it is built. Names match the code (`libs/common/.../topics.py`, `services/agents`). Boxes drawn with a dashed border are **design only** (described, not deployed). These render on GitHub; the same content is described in words in [ARCHITECTURE.md](ARCHITECTURE.md) and [EVENT_BACKBONE.md](EVENT_BACKBONE.md).

## 1. The system at a glance

Everything between services travels over Kafka. The API never calls an agent directly, and no agent calls another.

```mermaid
flowchart LR
    person([Person in a browser]) -->|HTTPS, cookies + CSRF token| api[API: FastAPI<br/>accounts, gate, SSE]
    api -->|user.event.received<br/>question.asked| kafka{{Kafka}}
    kafka --> proc[State processor]
    proc --> redis[(Redis<br/>per-person state,<br/>history, labs)]
    kafka --> t0[T0 service<br/>rules + calculators,<br/>no model]
    kafka --> agents[Agent graph<br/>one consumer group per role]
    agents -->|recommendation.released| kafka
    kafka -->|SSE steps and answer| api
    api --> pg[(Postgres<br/>accounts, sessions,<br/>consents, audit, corpus)]
    agents --> llm[[Model providers<br/>Groq, Gemini, OpenRouter<br/>synthetic or public text only]]
    agents --> pg
    kafka --> ops[Ops console and Ops MCP<br/>read-only]
    classDef design stroke-dasharray: 5 5
    keda[KEDA autoscaling]:::design -.-> agents
```

## 2. One question through the multi-agent graph (T3)

Each box is its own consumer group and can run as its own process. The Guardian is plain code, runs twice (at drafting and at release) and is the last gate.

```mermaid
flowchart TD
    q[question.asked] --> triage[Triage<br/>picks the tier and specialists]
    triage -->|run.planned| nut[Nutrition specialist]
    triage -->|run.planned| mov[Movement specialist]
    triage -->|run.planned| cond[Condition specialist]
    nut -->|advice.completed| agg[Aggregator<br/>code: waits for all parts<br/>or the deadline]
    mov -->|advice.completed| agg
    cond -->|advice.completed| agg
    agg -->|advice.bundle.ready<br/>partial on deadline| syn[Synthesizer<br/>writes prose from tool results]
    syn -->|recommendation.drafted| g1{Safety Guardian<br/>pure code}
    g1 -->|safety.blocked, one repair| syn
    g1 -->|safety.cleared| chk[Checker<br/>one bounded retry]
    chk -->|review.completed| rel[Release<br/>code: Guardian again,<br/>checkpoint per id]
    rel -->|needs a person| human[(Human review<br/>clinician console)]
    human -->|review.decided| rel
    rel -->|recommendation.released| out[Delivery: SSE to the person]
    tick[engagement.tick, daily per opted-in person] --> eng[Engagement: picks a nudge from a closed set]
    eng -->|nudge.planned| del[Delivery: policy checked again]
```

## 3. Who is trusted, and where the checks are

The model is untrusted. Numbers come from tools, text is verified in code, and the Guardian cannot be bypassed by a prompt.

```mermaid
flowchart LR
    subgraph untrusted [Untrusted]
        user([Question text]) 
        model[[Model output]]
        upload[Uploaded files]
    end
    user --> screen[Input screen<br/>blocklist: first layer only]
    screen --> agentgraph[Agent graph]
    agentgraph --> model
    model --> verify[Verification in code<br/>numbers vs tool results,<br/>citations, quotes]
    upload --> parse[Parsers<br/>size caps, defusedxml,<br/>nothing stored until confirmed]
    verify --> guardian{Safety Guardian<br/>fail closed}
    guardian -->|cleared| release[Release]
    guardian -->|blocked| safe[Safe fallback text<br/>or clinician review]
    api[API gate] -.->|deny by default,<br/>own data only,<br/>CSRF, roles| agentgraph
```

## 4. Delivery: retries, the dead-letter queue and the lease

At-least-once delivery with idempotent handlers. A crash anywhere re-delivers the record; the lease stops two workers doing the same event.

```mermaid
stateDiagram-v2
    [*] --> Received
    Received --> Leased: acquire lease (60 s in-flight TTL)
    Leased --> Done: handler ok, commit key, then commit offset
    Leased --> Retry1: transient failure, publish and flush
    Retry1 --> Leased: after 5 s
    Retry1 --> Retry2: fails again
    Retry2 --> Leased: after 30 s
    Retry2 --> Retry3: fails again
    Retry3 --> Leased: after 2 min
    Retry3 --> DLQ: fails again
    Received --> DLQ: poison message or deadline passed
    Done --> [*]
    DLQ --> [*]: original bytes kept for replay
```

## 5. Where it runs

The development stack is Docker Compose on one machine. The Azure run (three small VMs, 2026-11-08) was a measurement and was deleted afterwards; the Kubernetes parts are a design.

```mermaid
flowchart TB
    subgraph local [Local: Docker Compose, loopback only]
        k1[Kafka + Schema Registry]
        r1[(Redis)]
        p1[(Postgres + pgvector)]
        n1[(Neo4j)]
        s1[API, processor, T0, agents<br/>started with uv]
    end
    subgraph bench [Bench stack, separate ports]
        k2[Kafka 4.2.2]
        r2[(Redis)]
        p2[(Postgres, fsync off)]
        l2[Load and chaos harnesses]
    end
    subgraph azure [Azure test, deleted afterwards]
        vm0[VM 0: Kafka, Redis, load generator]
        vm1[VM 1: processors]
        vm2[VM 2: processors]
        kv[Key Vault via managed identity]
    end
    subgraph k8s [Design only]
        kd[Deployment per agent role<br/>+ KEDA ScaledObject on lag]:::design
    end
    classDef design stroke-dasharray: 5 5
    bench -.->|same code| azure
    local -.-> bench
    azure -.->|would become| k8s
```
