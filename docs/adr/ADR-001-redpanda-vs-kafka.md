# ADR-001: Use Redpanda Instead of Apache Kafka

| Property | Value |
|----------|-------|
| **ID** | ADR-001 |
| **Date** | 2026-03-03 |
| **Status** | ✅ Accepted |
| **Deciders** | Cloud Architecture Team |
| **Affected Components** | All messaging infrastructure (`ocean.telemetry.v1`, `market.prices.v1`, `ocean.alerts.v1`, `ocean.agent.decisions.v1`) |
| **Reviewed By** | (pending peer review) |

---

## Context

OceanTrust AI requires a **high-throughput, low-latency event streaming backbone** to support two fundamentally different workload profiles operating simultaneously:

1. **IoT Sensor Telemetry (`ocean.telemetry.v1`):** High-frequency, write-heavy traffic with bursts of up to ~10,000 sensor events per minute across all farms. Requires strict per-farm ordering guarantees and sub-100ms end-to-end latency to enable real-time alert detection.

2. **LLM Agent Context Reads (`market.prices.v1`, `ocean.alerts.v1`):** Read-heavy, latency-sensitive consumption. The LangGraph orchestrator must bootstrap agent context (latest fish prices, current alert state) in under 2 seconds to maintain a responsive decision loop.

Additionally, the project environment imposes the following constraints:
- **Deployment environment:** Single-region Kubernetes cluster on an initial tight infrastructure budget.
- **Operational simplicity:** The team is a small engineering group without dedicated Kafka operations expertise.
- **Development velocity:** Local development must be frictionless with no external dependencies.

We evaluated two candidate broker technologies: **Apache Kafka** (with Kafka KRaft mode) and **Redpanda**.

---

## Decision

**We will use Redpanda as the primary message broker for all OceanTrust AI Kafka-compatible workloads.**

---

## Rationale

### ✅ Factor 1: Single-Binary Architecture — Eliminating ZooKeeper/KRaft Complexity

Apache Kafka, even in its newer KRaft (no-ZooKeeper) mode, requires careful multi-process orchestration. The controller quorum, the broker processes, and Schema Registry are all separate services requiring independent lifecycle management, health checks, and storage provisioning.

Redpanda is a **single, self-contained binary** written in C++. It embeds the metadata quorum natively (equivalent to KRaft), which means:

| Aspect | Apache Kafka (KRaft) | Redpanda |
|--------|---------------------|----------|
| Processes to manage | Kafka Broker + Kafka Controller + Schema Registry | **1 binary** (broker + controller + schema registry built-in) |
| Kubernetes Pods per node | 3–4 | **1** |
| Config files | `server.properties` + `kraft.properties` + `schema-registry.properties` | `redpanda.yaml` |
| Bootstrap procedure | Complex KRaft quorum formation | Automatic |

For a portfolio project with limited K8s resources, this drastically reduces the operational surface area.

### ✅ Factor 2: Superior Tail Latency — Critical for Real-Time Alert Pipeline

The core alert pipeline of OceanTrust AI is latency-critical:

```
Sensor reading → Kafka Producer → ocean.telemetry.v1 → cg-alert-processor → ocean.alerts.v1 → LangGraph Trigger
```

Tolerated end-to-end latency budget: **< 500ms** from sensor capture to agent workflow start.

Redpanda achieves this through:
- **Thread-per-core architecture (Seastar framework):** Unlike Kafka's JVM-based threading model (which suffers from JVM GC pauses that can spike to 50–200ms+), Redpanda pins each shard to a dedicated CPU core with zero thread context switching overhead and completely predictable latency.
- **No JVM garbage collection pauses:** Kafka GC pauses are a known production pain point, especially under memory pressure. Redpanda is written in C++17 with manual memory management. There are zero GC events.

Benchmark reference (Redpanda published benchmarks on identical hardware, 3-broker cluster):

| Metric | Apache Kafka | Redpanda |
|--------|-------------|----------|
| Produce P99 latency (10k msg/s) | ~15–50ms | ~2–5ms |
| Produce P99.9 latency (10k msg/s) | ~100–300ms (GC spikes) | ~8–12ms |
| Consume end-to-end P99 | ~30–80ms | ~5–15ms |

For `ocean.telemetry.v1` which targets 30-second sensor intervals with alert SLA of 500ms, the Redpanda P99.9 profile provides a **comfortable latency margin** even under network jitter.

### ✅ Factor 3: Native Schema Registry — Enforcing Data Contracts Out of the Box

OceanTrust AI mandates schema validation for all messages (see [`docs/data-schemas.md`](../data-schemas.md)). With Apache Kafka, this requires deploying Confluent Schema Registry as a separate service with its own storage backend (Kafka-backed or standalone).

Redpanda ships with an **HTTP-compatible Schema Registry built into every broker node**. This means:
- Schema Registry is available immediately on `http://redpanda:8081` with zero additional Kubernetes Deployments.
- No separate storage backend for Schema Registry state.
- `rpk` CLI natively supports schema operations (`rpk registry schema create`, `rpk registry schema get`).

This allows enforcing `iot_sensor_event.schema.json` and `market_data_event.schema.json` contracts at the broker level from day one of development.

### ✅ Factor 4: Developer Experience — `rpk` CLI and Local Dev

Redpanda provides `rpk` (Redpanda Keeper), a single CLI tool that replaces the fragmented Kafka CLI ecosystem (`kafka-topics.sh`, `kafka-consumer-groups.sh`, `kafka-console-producer.sh`, etc.):

```bash
# Redpanda (rpk) — single unified CLI
rpk topic create ocean.telemetry.v1 --partitions 3 --replicas 3
rpk topic consume ocean.telemetry.v1 --num 5
rpk group describe cg-alert-processor

# vs. Apache Kafka — multiple scripts with different classpath issues
kafka-topics.sh --create --topic ocean.telemetry.v1 --partitions 3 --replication-factor 3 --bootstrap-server localhost:9092
kafka-console-consumer.sh --topic ocean.telemetry.v1 --max-messages 5 --bootstrap-server localhost:9092
kafka-consumer-groups.sh --describe --group cg-alert-processor --bootstrap-server localhost:9092
```

For local development via Docker Compose, Redpanda starts in **< 5 seconds** on `rpk container start`, compared to Kafka's multi-container orchestration (ZooKeeper/Controller + Broker warm-up) which can take 30–60 seconds.

### ✅ Factor 5: Full Kafka API Compatibility — Zero Vendor Lock-in

A frequent concern with adopting Redpanda over Kafka is vendor lock-in. Redpanda implements the **Kafka wire protocol natively** (Kafka API version 3.x compatible), meaning:

- All existing Kafka client libraries (Python `kafka-python`, `confluent-kafka`, Java, Go) work unchanged.
- All Kafka topic configurations (`cleanup.policy`, `retention.ms`, `min.insync.replicas`, etc.) are supported identically.
- If the project ever outgrows Redpanda or migrates to Confluent Cloud/MSK, all producers, consumers, and topic configurations remain valid with zero code changes.

This is verified in this project: all `rpk topic create` commands in [`docs/kafka-topics.md`](../kafka-topics.md) use standard Kafka topic configuration properties.

---

## Alternatives Considered

### Alternative A: Apache Kafka (KRaft mode, self-hosted)
- **Pros:** Industry standard, massive ecosystem, extensive operational documentation, AWS MSK / Confluent Cloud migration path.
- **Cons:** Requires managing separate KRaft controller quorum + broker processes. JVM GC pauses not acceptable for the < 500ms alert latency budget. Local dev requires docker-compose with 3+ containers and 30–60s startup time. No built-in Schema Registry.
- **Decision:** Rejected. Operational overhead disproportionate to project scale. GC latency incompatible with alert SLA.

### Alternative B: Apache Pulsar
- **Pros:** Native multi-tenancy, tiered storage, geo-replication built-in.
- **Cons:** Extreme architectural complexity (BookKeeper + ZooKeeper + Broker + Function Worker). Not Kafka API compatible — all clients would require Pulsar-specific libraries. Steeper learning curve with less portfolio recognition in the FinTech/IoT space.
- **Decision:** Rejected. Complexity far exceeds project requirements.

### Alternative C: NATS JetStream
- **Pros:** Extremely lightweight, sub-millisecond latency, excellent for IoT edge.
- **Cons:** Not Kafka API compatible (separate client ecosystem). Schema Registry not available. Limited ecosystem for stream processing integration (Flink, Spark). Less recognized in enterprise FinTech architecture portfolios.
- **Decision:** Rejected. Lack of Kafka API compatibility breaks integration with the planned Flink/Spark processing layer.

### Alternative D: Confluent Cloud (Managed Kafka)
- **Pros:** Zero operational overhead, enterprise Schema Registry, Kafka Streams included.
- **Cons:** Significant cost at scale. Does not demonstrate infrastructure engineering depth in a portfolio context. Requires external internet access for local development.
- **Decision:** Rejected for initial portfolio phase. Reserved as a future migration path if the project is productionized.

---

## Trade-offs & Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Redpanda missing a Kafka API feature needed later | Low | Medium | Full Kafka API compatibility is a Redpanda core guarantee. Track [Redpanda Kafka compatibility matrix](https://docs.redpanda.com/current/develop/kafka-clients/). |
| Smaller community than Apache Kafka | Medium | Low | Kafka client libraries are used everywhere; the broker difference is transparent to application code. |
| Performance benchmarks were vendor-published | Low | Low | Independent benchmarks (e.g., by Gunnar Morling, 2023) confirm Redpanda tail latency advantage in single-region setups similar to ours. |
| Future migration to MSK/Confluent Cloud | Low | Low | Full Kafka wire protocol compatibility ensures zero application-level migration cost. |

---

## Consequences

- **`bin/docker/docker-compose.yml`** will use `docker.redpanda.com/redpandadata/redpanda` as the broker image (not `confluentinc/cp-kafka`).
- **`bin/kubernetes/`** will use the [Redpanda Helm chart](https://github.com/redpanda-data/helm-charts) for cluster deployment.
- **Schema Registry** endpoint will be `http://redpanda-service:8081` with no additional deployment.
- **All `rpk` CLI commands** in `docs/kafka-topics.md` serve as the authoritative topic provisioning specification, executable against both local (Docker) and production (K8s) Redpanda clusters.
- All application code must use standard **Kafka client libraries only** (no Redpanda-specific SDKs) to preserve portability.

---

## References

- [Redpanda vs. Kafka Architecture Comparison — Redpanda Docs](https://docs.redpanda.com/current/get-started/architecture/)
- [Redpanda Kafka Compatibility Matrix](https://docs.redpanda.com/current/develop/kafka-clients/)
- [Seastar Framework — Thread-per-core](http://seastar.io/)
- [OceanTrust ADR Log](./README.md)
