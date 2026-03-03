# Kafka Topic Strategy — OceanTrust AI

> **Maintainer:** Cloud Architecture Team
> **Version:** 1.1.0
> **Last Updated:** 2026-03-03
> **Broker:** Redpanda (Kafka API v3.x compatible)

This document defines the complete messaging topology for OceanTrust AI. It covers topic naming conventions, partition strategy, retention/compaction policies, replication factors, and consumer group design. All configurations are expressed as broker-level topic properties and must be applied via the `rpk` CLI or Terraform Redpanda provider during infrastructure provisioning.

---

## Table of Contents

1. [Topic Naming Convention](#1-topic-naming-convention)
2. [Global Broker Defaults](#2-global-broker-defaults)
3. [Topic: `ocean.telemetry.v1`](#3-topic-oceantemetryv1)
4. [Topic: `market.prices.v1`](#4-topic-marketpricesv1)
5. [Topic: `ocean.alerts.v1`](#5-topic-oceanalertsv1)
6. [Topic: `ocean.agent.decisions.v1`](#6-topic-oceanagentdecisionsv1)
7. [Consumer Group Design](#7-consumer-group-design)
8. [Monitoring & Observability](#8-monitoring--observability)
9. [Topic Creation Commands](#9-topic-creation-commands)

---

## 1. Topic Naming Convention

All topics follow a strict naming schema to support multi-environment deployments and automated schema registry subject mapping.

```
{domain}.{entity}.v{major-version}
```

| Segment | Rules | Example |
|---------|-------|---------|
| `domain` | lowercase, max 15 chars | `ocean`, `market` |
| `entity` | lowercase, snake_case | `telemetry`, `prices`, `alerts` |
| `v{N}` | integer major version only | `v1`, `v2` |

**Environment prefixing:** Prepend the environment short-code for non-production topics:

```
dev.ocean.telemetry.v1
stg.ocean.telemetry.v1
ocean.telemetry.v1            ← production (no prefix)
```

**Schema Registry Subject convention:**
```
{topic-name}-value
```
Example: `ocean.telemetry.v1-value` → maps to `iot_sensor_event.schema.json v1.0.0`

---

## 2. Global Broker Defaults

These defaults are set at the Redpanda cluster level and apply to all topics unless overridden at the topic level.

```properties
# Redpanda cluster-level defaults (redpanda.yaml)
log.retention.ms=604800000          # 7 days default retention
log.segment.bytes=536870912         # 512 MB per segment
log.retention.check.interval.ms=300000  # 5 min compaction check interval
default.replication.factor=3
min.insync.replicas=2
auto.create.topics.enable=false     # NEVER allow auto-creation in production
compression.type=lz4                # LZ4 for balanced throughput/CPU
```

---

## 3. Topic: `ocean.telemetry.v1`

**Purpose:** Ingests raw IoT sensor readings from all salmon farm cage probes at 30-second cadence.
**Schema:** [`iot_sensor_event.schema.json`](../schemas/iot_sensor_event.schema.json)
**Kafka Partition Key:** `farm_id` (string, e.g. `NO-FARM-0047`)

### 3.1 Configuration

```properties
# Topic-level configuration for ocean.telemetry.v1
partitions=3
replication.factor=3
min.insync.replicas=2

# --- Retention Policy (DELETE) ---
# Raw telemetry retained for 7 days for reprocessing and backfill.
cleanup.policy=delete
retention.ms=604800000              # 7 days
retention.bytes=-1                  # No size limit — time-based only
segment.ms=3600000                  # Roll new segment every 1 hour

# --- Performance ---
compression.type=lz4
max.message.bytes=1048576           # 1 MB max per message
```

### 3.2 Partition Strategy — Why 3 Partitions with `farm_id` as Key

> **Requirement:** Events from the **same farm** must be processed **in strict sequential order** to prevent false alert triggers caused by out-of-order readings (e.g., an old low-O₂ reading arriving after a recovery event).

**Design decision:**

| Farm ID (Key) | Hash (`murmur2`) | Assigned Partition |
|---------------|------------------|--------------------|
| `NO-FARM-0001` | … | Partition 0 |
| `NO-FARM-0047` | … | Partition 1 |
| `NO-FARM-0099` | … | Partition 2 |

- All events from a given `farm_id` land on the **same partition**, always processed by the **same consumer instance**. This provides a total ordering guarantee **per farm** without requiring global ordering (which would require 1 partition and eliminate parallelism).
- **Parallelism:** Up to **3 concurrent consumer instances** can process telemetry in parallel, each owning one partition. Adding more consumer instances beyond 3 yields no benefit without partition expansion.
- **Scaling policy:** Partition count should scale with `ceil(max_farms / target_farms_per_consumer)`. Current target: ~15 farms per consumer → 3 partitions supports up to 45 active farms.

### 3.3 Consumer Groups

| Consumer Group ID | Purpose | Instances |
|-------------------|---------|-----------|
| `cg-alert-processor` | Threshold evaluation, emits to `ocean.alerts.v1` | 3 (one per partition) |
| `cg-timeseries-writer` | Writes to TimescaleDB / InfluxDB for dashboards | 3 |
| `cg-ml-feature-pipeline` | Feature extraction for ML model retraining | 3 |

### 3.4 Message Flow Diagram

```
Sensor (SENSOR-NO-FARM47-CAGE03-UNIT02)
        │
        │  key="NO-FARM-0047"
        ▼
┌─────────────────────────────────────────┐
│      ocean.telemetry.v1                 │
│                                         │
│  Partition 0 │ Partition 1 │ Partition 2│
│  [FARM-0001] │ [FARM-0047] │ [FARM-0099]│
│  [FARM-0023] │ [FARM-0062] │ [FARM-0101]│
│      ...     │     ...     │     ...    │
└─────┬────────────────┬────────────────┬─┘
      │                │                │
      ▼                ▼                ▼
   Worker-0         Worker-1         Worker-2
(cg-alert-processor — one instance per partition)
```

### 3.5 Alert Threshold Reference (for Consumer Logic)

| Field | WARNING | CRITICAL |
|-------|---------|----------|
| `dissolved_oxygen_mg_l` | < 9.0 | < 7.0 |
| `temperature_celsius` | > 16.0°C | > 18.0°C |
| `lice_count_per_fish` | > 0.3 | > 0.5 (regulatory limit) |
| `chlorophyll_a_ug_l` | > 7.0 | > 10.0 (algae bloom) |
| `mortality_count_24h` | > 20 | > 50 |

---

## 4. Topic: `market.prices.v1`

**Purpose:** Carries fish commodity price ticks from Oslo Fish Pool and other market data providers. Consumed by the Commercial Agent for harvest/sell decision support.
**Schema:** [`market_data_event.schema.json`](../schemas/market_data_event.schema.json)
**Kafka Partition Key:** Composite — `{species}:{product_form}` (e.g. `ATLANTIC_SALMON:FRESH_HOG`)

### 4.1 Configuration

```properties
# Topic-level configuration for market.prices.v1
partitions=8
replication.factor=3
min.insync.replicas=2

# --- Compaction Policy ---
# Compact mode retains ONLY the latest price record per (species, product_form) key.
# This makes market.prices.v1 act as a reliable last-known-price store.
cleanup.policy=compact
min.cleanable.dirty.ratio=0.1      # Compact aggressively (10% dirty tolerance)
segment.ms=3600000                  # Roll segments every 1 hour for compaction eligibility
delete.retention.ms=86400000        # Keep tombstone markers for 1 day
min.compaction.lag.ms=0             # Allow compaction of messages immediately

# --- Performance ---
compression.type=lz4
max.message.bytes=524288            # 512 KB max per message
```

### 4.2 Compaction Strategy — Why `cleanup.policy=compact`

> **Requirement:** Agents querying market prices need the **latest available price** for a given commodity. Historical price tick storage is handled separately (TimescaleDB). The Kafka topic must act as a **fast, always-current price cache**, not a historical log.

**How log compaction works for this topic:**

```
Timeline → (oldest) ─────────────────────────────→ (newest)

Key: ATLANTIC_SALMON:FRESH_HOG
  [tick@10:00, price=85.50] [tick@10:05, price=86.00] [tick@10:10, price=87.50]
                                                                ↑
                                      After compaction: only THIS is retained

Key: BLUEFIN_TUNA:FRESH_HOG
  [tick@10:00, price=2400.00] [tick@10:05, price=2450.00]
                                         ↑
                       After compaction: only THIS is retained
```

**Guarantees:**
- A consumer starting from offset 0 after compaction reads the **current state of the world** (all instruments at their latest price) without replaying thousands of historical ticks.
- The LangGraph Commercial Agent can bootstrap its price context in **one pass** of this compacted topic on startup.

### 4.3 Partition Sizing — 8 Partitions

The composite key `{species}:{product_form}` yields a bounded cardinality:
- 8 species × 7 product forms = **56 possible distinct keys**
- 8 partitions ensures consistent spread with the Murmur2 hash function for 56 keys.
- Each market data consumer handles ≤ 7 price streams, keeping per-partition CPU overhead minimal.

### 4.4 Consumer Groups

| Consumer Group ID | Purpose | Instances |
|-------------------|---------|-----------|
| `cg-commercial-agent-feed` | Provides latest price context to the LangGraph Commercial Agent | 8 |
| `cg-timeseries-market-writer` | Appends all ticks to TimescaleDB for historical analysis | 8 |
| `cg-price-alert-monitor` | Detects extreme price swings (> ±5% in 1h) | 8 |

---

## 5. Topic: `ocean.alerts.v1`

**Purpose:** Fan-out topic for all threshold breach events generated by the `cg-alert-processor` consumer group reading from `ocean.telemetry.v1`. Triggers the LangGraph debate workflow.

```properties
partitions=6
replication.factor=3
cleanup.policy=delete
retention.ms=172800000              # 48 hours — alerts are time-sensitive, not long-lived
compression.type=lz4
max.message.bytes=262144            # 256 KB
```

**Key:** `farm_id` — preserves ordering of alerts per farm, matches `ocean.telemetry.v1` partition assignment.

---

## 6. Topic: `ocean.agent.decisions.v1`

**Purpose:** Stores the final structured output of the LangGraph Judge Agent after each debate cycle. This is the auditable decision log consumed by the operator dashboard and the PDF report generator.

```properties
partitions=3
replication.factor=3
cleanup.policy=delete
retention.ms=7776000000             # 90 days — decision records are compliance artifacts
compression.type=gzip               # GZIP for higher compression ratio on JSON decision reports
max.message.bytes=5242880           # 5 MB — decision reports include cited RAG chunks
```

**Key:** `farm_id` — all decisions for a farm remain on one partition for ordered audit trail.

---

## 7. Consumer Group Design

### 7.1 Offset Management Policy

| Policy | Rule |
|--------|------|
| **Auto-commit** | Disabled (`enable.auto.commit=false`) for all groups |
| **Manual commit** | Commit only after successful downstream write (at-least-once delivery) |
| **Reset policy** | `auto.offset.reset=earliest` for processing groups, `latest` for real-time dashboard groups |
| **Idle timeout** | `session.timeout.ms=30000` (30s), `heartbeat.interval.ms=10000` (10s) |

### 7.2 Consumer Group Summary

```
ocean.telemetry.v1  ──┬──► cg-alert-processor       → ocean.alerts.v1
                      ├──► cg-timeseries-writer      → TimescaleDB
                      └──► cg-ml-feature-pipeline    → Feature Store (S3)

market.prices.v1    ──┬──► cg-commercial-agent-feed  → LangGraph Commercial Agent
                      └──► cg-timeseries-market-writer → TimescaleDB

ocean.alerts.v1     ──┬──► cg-langgraph-orchestrator → LangGraph Debate Workflow
                      └──► cg-ops-notification        → PagerDuty / Slack Webhook

ocean.agent.decisions.v1 ──► cg-dashboard-writer     → Operator UI WebSocket
                          └─► cg-report-generator     → PDF Audit Report Service
```

---

## 8. Monitoring & Observability

All topics are scraped by Prometheus via the Redpanda `/metrics` endpoint. The following metrics are mandatory alerts in the Grafana dashboard (see `bin/kubernetes/monitoring/`):

| Metric | Alert Threshold | Severity |
|--------|-----------------|----------|
| `kafka_consumer_group_lag` (per group/partition) | > 1000 messages | WARNING |
| `kafka_consumer_group_lag` (per group/partition) | > 10000 messages | CRITICAL |
| `redpanda_kafka_request_latency_seconds_p99` | > 200ms | WARNING |
| `redpanda_storage_disk_free_bytes` | < 20% capacity | CRITICAL |
| `kafka_topic_partitions_under_replicated` | > 0 | CRITICAL |

---

## 9. Topic Creation Commands

Use `rpk` (Redpanda CLI) to create all topics idempotently during infrastructure provisioning. These commands are also embedded in the Terraform Redpanda provider resource definitions in `bin/terraform/`.

```bash
# ── ocean.telemetry.v1 ─────────────────────────────────────────────────────
rpk topic create ocean.telemetry.v1 \
  --partitions 3 \
  --replicas 3 \
  --topic-config cleanup.policy=delete \
  --topic-config retention.ms=604800000 \
  --topic-config segment.ms=3600000 \
  --topic-config compression.type=lz4 \
  --topic-config min.insync.replicas=2 \
  --topic-config max.message.bytes=1048576

# ── market.prices.v1 ──────────────────────────────────────────────────────
rpk topic create market.prices.v1 \
  --partitions 8 \
  --replicas 3 \
  --topic-config cleanup.policy=compact \
  --topic-config min.cleanable.dirty.ratio=0.1 \
  --topic-config segment.ms=3600000 \
  --topic-config delete.retention.ms=86400000 \
  --topic-config min.compaction.lag.ms=0 \
  --topic-config compression.type=lz4 \
  --topic-config min.insync.replicas=2 \
  --topic-config max.message.bytes=524288

# ── ocean.alerts.v1 ───────────────────────────────────────────────────────
rpk topic create ocean.alerts.v1 \
  --partitions 6 \
  --replicas 3 \
  --topic-config cleanup.policy=delete \
  --topic-config retention.ms=172800000 \
  --topic-config compression.type=lz4 \
  --topic-config min.insync.replicas=2

# ── ocean.agent.decisions.v1 ─────────────────────────────────────────────
rpk topic create ocean.agent.decisions.v1 \
  --partitions 3 \
  --replicas 3 \
  --topic-config cleanup.policy=delete \
  --topic-config retention.ms=7776000000 \
  --topic-config compression.type=gzip \
  --topic-config min.insync.replicas=2 \
  --topic-config max.message.bytes=5242880

# ── Verify all topics ────────────────────────────────────────────────────
rpk topic list
rpk topic describe ocean.telemetry.v1
rpk topic describe market.prices.v1
```

### Topic Configuration Summary Table

| Topic | Partitions | Cleanup Policy | Retention | Key | Compression |
|-------|-----------|----------------|-----------|-----|-------------|
| `ocean.telemetry.v1` | 3 | `delete` | 7 days | `farm_id` | `lz4` |
| `market.prices.v1` | 8 | `compact` | ∞ (last value) | `species:product_form` | `lz4` |
| `ocean.alerts.v1` | 6 | `delete` | 48 hours | `farm_id` | `lz4` |
| `ocean.agent.decisions.v1` | 3 | `delete` | 90 days | `farm_id` | `gzip` |
