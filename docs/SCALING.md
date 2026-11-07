# Scaling VitalMesh: what was measured, what is arithmetic, what is design

This page says exactly how far the scale claim goes. Every number is labelled **measured**,
**arithmetic** (computed from measured numbers), **assumption** (a number we chose) or **design**
(not built or not tested). Nothing here is a test with a million real users.

> **How to read every measured number:** synthetic people, a simulated model where a model is
> involved, one 16-core laptop running Docker Desktop, time compressed. The load tools refuse to run
> against anything but the isolated benchmark stack.

## 1. What was measured

### The event pipeline (Kafka -> processor -> Redis)

Method: `scripts/load_pipeline.py`. Each run resets the benchmark topics, starts real processor
processes in their own consumer group, publishes events for synthetic people and then checks that
nothing was lost or duplicated: all offsets committed, one history point per measurement, one state
record per person.

| processes | events/s (saturated) | scaling |
|---|---|---|
| 1 | 179 | 100% |
| 2 | 357 | 100% |
| 3 | 494 | 92% |
| 6 | 878 | 82% |

People replayed through the pipeline with every correctness check green and no dead letters:
10,000 (a too-short run, so not a throughput figure), 100,000 (860 events/s on 6 processes) and
**1,000,000 (786 events/s on 6 processes, 711 bytes of Redis per person, 691 MB in total)**. Two
earlier 1M attempts on Kafka 4.0.0 stalled because of a broker bug (KAFKA-19888); they were
discarded and the benchmark broker moved to 4.2.2.

### The multi-agent graph (triage, three specialists, aggregator, synthesizer, Guardian, checker, release gate)

Method: `scripts/load_agents.py`, with a **simulated model** that answers with fixed text after a
latency you choose. It measures the graph, not answer quality, and spends no model quota.

| model latency | processes per role | questions/s | median latency (burst of all questions) |
|---|---|---|---|
| 0 ms | 1 | 29.1 | 13.2 s |
| 800 ms per call | 1 | 0.62 | 99.7 s |
| 800 ms per call | 2 | 0.87 | 62.6 s |
| 800 ms per call | 3 | 1.51 | 34.3 s |

With a free model the graph costs about 142 ms of CPU per question across ten roles, so the graph
is not the limit; **the model is**. With a slow model the limit is how many specialist handlers run
at once. The agent topics have 3 partitions locally, so 3 processes per role is the local maximum
(the production profile has more partitions).

### Failure tests (chaos), agent graph, 100 questions each

| failure | result |
|---|---|
| kill a specialist, the aggregator (restarted later) and the release step mid-run | 100 of 100 questions finished exactly once, 0 dead letters |
| restart the Kafka broker mid-run | 100 of 100 exactly once, 0 dead letters (cost: latency) |
| the model fails every call for 30 s | 100 of 100 ended exactly once, 0 dead letters; 30 specialist answers failed safely, 18 of 80 released answers were flagged partial, 7 were blocked by the Guardian rather than released |

## 2. The capacity model (arithmetic)

`uv run python scripts/capacity_model.py` prints every input with its tag. With our *assumed* usage
(4 events per person per day, 10x peak, 30% active a day, 0.1 questions per person per day, 5% of
questions needing the full graph, 6,000 tokens per run), one million people are roughly:

| quantity | value | tag |
|---|---|---|
| events | 46/s average, 463/s at peak | arithmetic |
| processor processes at the measured rate | 4 | arithmetic |
| Redis memory | 0.7 GB | arithmetic (711 B per person, measured) |
| API requests | about 350/s at peak | arithmetic |
| model tokens | about 208,000 per minute at peak (27 free-tier keys) | arithmetic from assumptions |

**Named bottlenecks, in the order they would bite:** the model quota first; then one Redis node's
operation rate (assumed, not measured); then Postgres connections for the account layer; then Kafka
partitions. A rough floor of six VMs is about 420 USD a month at list price (arithmetic).

**What this does not show:** the account layer, the dashboards and real model behaviour at that
scale were not run at a million people; the one-million figure for the whole product is
arithmetic from measured rates.

## 3. Design (not deployed): lag-based autoscaling with KEDA

Status: **design only.** Running it needs a Kubernetes cluster, which is outside the budget and
scope of this project phase; the numbers below are arithmetic from the measurements above.

* Each agent role is its own Deployment and consumes its topics with one consumer group, so a role
  scales independently (the graph already runs this way: one process per role).
* Each Deployment gets a KEDA `ScaledObject` with a **Kafka trigger** (consumer group, topic, lag
  threshold). Replicas = ceil(lag / threshold), never more than the **partition count**, because a
  replica beyond that sits idle. So partitions are the ceiling, and the production topic profile
  (48 to 96 partitions for events, fewer for the agent topics) sets how far a role can scale.
* Lag threshold per replica = measured rate per replica x the wait we accept. Examples (arithmetic):
  a processor handles about 131 events/s, so with a 10 s acceptable wait the threshold is about
  1,300 messages; a specialist with an 800 ms model handles about 0.6 questions/s, so with a 30 s
  wait the threshold is about 19.
* Model-bound roles also scale on **budget**: adding replicas cannot help when the model quota is
  spent, so a second trigger reads the token-budget counter in Redis and holds the replica count
  when the budget is exhausted (the load monitor already sheds tiers in that case).
* The Guardian and release roles keep a minimum of one replica; only the engagement and delivery
  roles may scale to zero. Scale-down uses a stabilisation window so a burst does not make the
  replica count flap.
* Not covered: the Kafka brokers, Redis and Postgres scale differently (partitions and brokers,
  sharding or a cluster, read replicas and pooling).

## 4. Secrets and transport (Key Vault and HTTPS)

* **Key Vault.** The API reads its signing secret from Azure Key Vault when
  `VITALMESH_AUTH_SECRET_KEYVAULT=<vault>/<secret>` is set (and `VITALMESH_TOKEN_KEY_KEYVAULT`
  for the Google token key). It uses the machine's managed identity through the instance metadata
  service, so no password or key is stored. A vault failure stops the process; it never falls back
  to a random or file secret. Names are validated before they go into a URL.
* **HTTPS.** An `https://` `VITALMESH_BASE_URL` turns on Secure cookies (and so HSTS)
  automatically; the mistaken combination of an https address with `VITALMESH_SECURE_COOKIES=0`
  refuses to start. A public certificate needs a domain name and an internet-reachable port 80,
  so the Azure test uses a self-signed certificate on a port open only to the operator's address.
  That proves the configuration (Secure cookies, HSTS, vault-loaded secret), not a public
  certificate.

## 5. Reproducing it

```
docker compose -f infra/docker-compose.bench.yml up -d --wait
export VITALMESH_KAFKA_BOOTSTRAP=127.0.0.1:9192 VITALMESH_SCHEMA_REGISTRY_URL=http://127.0.0.1:8181 \
       VITALMESH_REDIS_URL=redis://127.0.0.1:6390/0
uv run python scripts/load_pipeline.py --events 100000 --users 100000 --procs 6 --no-probe --json out.json
uv run python scripts/load_agents.py --questions 120 --procs 3 --sim-ms 800 --json out.json
uv run python scripts/load_agents.py --questions 100 --procs 2 --rate 0.7 --sim-ms 800 \
       --chaos "kill:nutrition@30,broker@60" --json out.json
uv run python scripts/capacity_model.py
docker compose -f infra/docker-compose.bench.yml down -v
```
