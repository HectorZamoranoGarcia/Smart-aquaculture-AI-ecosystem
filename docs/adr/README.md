# Architecture Decision Records — OceanTrust AI

This directory contains all Architecture Decision Records (ADRs) for the OceanTrust AI platform. ADRs document significant technical choices — what was decided, why it was decided that way, and what alternatives were rejected.

## ADR Format

Each ADR follows the [MADR (Markdown Any Decision Record)](https://adr.github.io/madr/) template with additional fields for risk assessment and project-specific properties.

## Record Index

| ID | Title | Status | Date |
|----|-------|--------|------|
| [ADR-001](ADR-001-redpanda-vs-kafka.md) | Use Redpanda Instead of Apache Kafka | ✅ Accepted | 2026-03-03 |
| [ADR-002](ADR-002-circuit-breaker-embeddings.md) | Circuit Breaker for OpenAI Embedding API Calls | ✅ Accepted | 2026-03-03 |
| [ADR-003](ADR-003-at-least-once-orchestrator.md) | At-Least-Once Delivery Semantics in the Agent Orchestrator | ✅ Accepted | 2026-03-03 |
| [ADR-004](ADR-004-embedding-dimensions-ollama.md) | 768-Dimension Embeddings for Ollama Fallback Compatibility | ✅ Accepted | 2026-03-03 |

## ADR Lifecycle

```
Proposed → Accepted → Deprecated → Superseded
```

- **Proposed:** Under review by the architecture team
- **Accepted:** Approved and actively governing the design
- **Deprecated:** No longer relevant but retained for history
- **Superseded:** Replaced by a newer ADR (linked in the record)

## How to Create a New ADR

1. Copy the template from an existing ADR.
2. Number it sequentially: `ADR-{NNN}-{short-title}.md`
3. Set `Status: Proposed` and assign a date.
4. Submit for peer review via pull request.
5. On approval, update status to `Accepted` and add to this index.
