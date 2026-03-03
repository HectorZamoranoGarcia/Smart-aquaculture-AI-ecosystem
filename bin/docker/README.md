# Async Producer — Local Infrastructure

This document describes how to start and validate the local development environment.

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Docker Desktop | ≥ 4.x | Container runtime |
| `rpk` CLI _(optional)_ | ≥ 24.x | Direct cluster management |

## Quick Start

```bash
# 1. Start Redpanda + Console
make up

# 2. Wait for the cluster and create all topics
make provision

# 3. Open the Console UI
open http://localhost:8080
```

## Services

| Service | Internal Address | External Port | Purpose |
|---|---|---|---|
| `redpanda` | `redpanda:9092` | `9092` | Kafka-compatible broker |
| `redpanda` | `redpanda:9644` | `9644` | Admin REST API |
| `redpanda` | `redpanda:8081` | `8081` | Schema Registry |
| `console`  | — | `8080` | Web UI |

## Provisioned Topics — OceanTrust.ai Domains

| Topic | Partitions | Replication | Retention | Partition Key |
|---|---|---|---|---|
| `salmon.farm.sensor.telemetry` | 12 | 1 | 30 days | `farm_id` |
| `market.oslo.fish.prices` | 8 | 1 | 7 days | `species:product_form` |
| `dlq.events.failed` | 4 | 1 | 7 days | — |
| `llm.commands.dispatch` | 6 | 1 | 7 days | — |
| `llm.responses.processed` | 6 | 1 | 7 days | — |

> **Source schemas:** `schemas/iot_sensor_event.schema.json` · `schemas/market_data_event.schema.json`

## Environment Variables

Copy `.env.example` to `.env` and adjust values for your setup:

```bash
cp .env.example .env
```

## Teardown

```bash
# Stop without losing data
make down

# Full reset (removes volumes)
make clean
```
