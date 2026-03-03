# ADR-002 — Circuit Breaker para las llamadas a la API de Embeddings

| Campo       | Valor                                          |
|-------------|------------------------------------------------|
| **ID**      | ADR-002                                        |
| **Título**  | Adopción del patrón Circuit Breaker para la API de embeddings de OpenAI |
| **Estado**  | Aprobado                                       |
| **Fecha**   | 2026-03-03                                     |
| **Autores** | Cloud Architecture Team                        |
| **Depende de** | ADR-001 (Redpanda), ADR-003 (At-Least-Once) |

---

## Contexto

El **Vector Ingestion Worker** consume el tópico Redpanda `ocean.telemetry.v1` a alta frecuencia (~10.000 eventos/min en pico). Por cada lote de eventos, realiza llamadas síncronas a la API de embeddings de OpenAI (`text-embedding-3-large`). Este acoplamiento introduce un riesgo de punto único de fallo:

- **HTTP 429 (Rate Limit):** OpenAI impone cuotas de tokens por minuto. Un burst de 500 eventos simultáneos puede superar el límite y provocar una cascada de reintentos.
- **HTTP 5xx / Timeout de red:** Cualquier degradación en la infraestructura de OpenAI bloquea el consumer loop de Kafka, incrementando el consumer lag indefinidamente.
- **Efecto cascada:** Sin aislamiento, el Worker reintenta en loop, agotando el pool de conexiones y potencialmente causando el reinicio del pod por Kubernetes (`CrashLoopBackOff`).

El Sistema de Alerta OceanTrust tiene un **SLA de 500ms** desde que el sensor emite la lectura hasta que el Orquestador inicia el debate. Un Kafka consumer lag superior a 5.000 mensajes invalida este SLA.

---

## Decisión

Se implementa el **patrón Circuit Breaker** con tres estados (`CLOSED` → `OPEN` → `HALF_OPEN`) utilizando la librería `tenacity` para los reintentos y una variable de estado gestionada dentro del Worker.

**Parámetros del Circuit Breaker:**

| Parámetro | Valor | Justificación |
|-----------|-------|---------------|
| Umbral de apertura (`failure_threshold`) | 5 fallos consecutivos (5xx / 429) | Equivale a ~2.5 segundos de reintentos exponenciales antes de abrir |
| Tiempo de espera en estado `OPEN` (`recovery_timeout`) | 30 segundos | Suficiente para que OpenAI resuelva un throttling transitorio típico |
| Petición de prueba en `HALF_OPEN` | 1 petición con 1 solo texto | Minimiza el costo de la prueba de recuperación |
| Backoff de reintentos (dentro de `CLOSED`) | Exponencial: `min(1 * 2^attempt, 10s)` con jitter | Reduce la presión sobre la API en ventanas de throttling |

**Comportamiento en estado `OPEN` — Fallback a Ollama local:**

Cuando el Circuit Breaker está `OPEN`, el Worker **no bloquea la ingesta**. En su lugar, rutea la petición de embedding al servidor **Ollama local** (`nomic-embed-text`, 768 dimensiones) que corre en el mismo cluster de Kubernetes. Los vectores resultantes se escriben en la colección temporal `telemetry_vectors_fallback` (dim=768) para evitar incompatibilidad de esquema con la colección principal (dim=3072).

**El Consumer Offset de Kafka sólo se commitea después de recibir el ACK de Qdrant** — independientemente de si se usó el modelo principal o el fallback. Esto preserva la semántica At-Least-Once definida en ADR-003.

---

## Alternativas consideradas

| Alternativa | Razón del rechazo |
|-------------|-------------------|
| **Retry ilimitado sin Circuit Breaker** | Las APIs de OpenAI en outage pueden tardar minutos; el retry loop bloquea el thread del consumer indefinidamente, acumulando lag en Kafka hasta superar el `max.poll.interval.ms` y provocar un rebalanceo innecesario. |
| **Dead Letter Queue (DLQ) en caso de fallo de embedding** | El DLQ almacena el evento fallido pero los eventos de telemetría pierden valor temporal rápidamente (datos de hace 60s son poco accionables para el agente). La latencia de re-procesamiento es inaceptable. |
| **Sincronismo total (bloquear consumer hasta recuperación)** | El consumer Kafka tiene un timer de `max.poll.interval.ms` (60s). Si no se llama a `poll()` dentro de ese intervalo, el broker asume que el consumer ha fallado y reasigna las particiones, reiniciando el procesamiento desde el último offset commiteado. |

---

## Consecuencias

**Positivas:**
- El consumer de Kafka **nunca se bloquea** más de 30 segundos por una caída de OpenAI.
- El lag de `cg-telemetry-vectorizer` se mantiene bajo incluso during outages del proveedor de IA.
- La trazabilidad está preservada: los puntos en `telemetry_vectors_fallback` están marcados con `embedding_model: nomic-embed-text` en su payload.

**Negativas / trade-offs:**
- Los vectores en `telemetry_vectors_fallback` (768 dims) y los de `telemetry_vectors` (3072 dims) no son comparables directamente. Los agentes deben conocer en qué colección buscar.
- Añade complejidad operacional: el equipo de ML debe monitorear el ratio de uso de `fallback` como indicador de salud de la integración con OpenAI.

---

## Estado de implementación

- `tenacity` declarado en `pyproject.toml` como dependencia core.
- Lógica de Circuit Breaker pendiente de implementación en `src/ingestion/vector_worker.py`.
- Colección `telemetry_vectors_fallback` (dim=768) ya provisionada en `bin/scripts/provision_qdrant.sh`.
