# ADR-003 — Semántica At-Least-Once en el Orquestador de Agentes

| Campo       | Valor                                          |
|-------------|------------------------------------------------|
| **ID**      | ADR-003                                        |
| **Título**  | Garantía de entrega At-Least-Once para el pipeline de despacho de agentes |
| **Estado**  | Aprobado                                       |
| **Fecha**   | 2026-03-03                                     |
| **Autores** | Cloud Architecture Team                        |
| **Depende de** | ADR-001 (Redpanda)                          |

---

## Contexto

El Orquestador LangGraph consume el tópico `ocean.alerts.v1` (consumer group `cg-alert-processor`) y, para cada alerta recibida, inicia un ciclo de debate entre los tres agentes (Biólogo, Comercial, Juez). Una vez finalizado el debata, el veredicto se publica en `ocean.agent.decisions.v1`.

Existe una **ventana de fallo crítica** entre dos eventos:
1. El Consumer lee el mensaje de alerta y el Orquestador inicia el debate (`Poll → Start Debate`).
2. El débate LangGraph completa y el veredicto se publica en Kafka (`End Debate → Produce Verdict`).

Si el pod del Orquestador crashea entre estos dos puntos, ¿se pierde la alerta?

### Opciones de semántica de entrega en Kafka

| Semántica | Mecanismo | Riesgo |
|-----------|-----------|--------|
| **At-Most-Once** | Commit offset inmediatamente al hacer `poll()` | Si el pod falla durante el debate, la alerta se pierde. Inaceptable para un sistema regulatorio. |
| **At-Least-Once** | Commit offset sólo tras producir el veredicto a `ocean.agent.decisions.v1` | En caso de fallo, la alerta se reprocesa. El debate se reinicia. Duplicados posibles. |
| **Exactly-Once** | Kafka Transactions (KIP-98) | Requiere coordinación transaccional Producer+Consumer en el mismo proceso; aumenta la complejidad y la latencia P99 en ~15-30ms. No compatible con Redpanda sin configuración adicional. |

En el dominio aquícola, **perder una alerta de crisis de oxígeno** (Art. 12 Akvakulturloven) tiene consecuencias legales y de bienestar animal. Un **debate duplicado** es indeseable pero inofensivo: el segundo debate sobreescribirá el veredicto en `ocean.agent.decisions.v1` con el mismo `debate_id` gracias al campo `event_id` (idempotente por diseño).

---

## Decisión

Se adopta **At-Least-Once** con `enable.auto.commit=False` y commit manual del offset **sólo tras el ACK de producción del veredicto**.

**Secuencia de commit controlada:**

```
1. poll()                → Recibir alerta de ocean.alerts.v1
2. start_debate()        → Iniciar StateGraph LangGraph
3. ... debate rounds ... → Agentes deliberan (puede durar 8-30s)
4. produce_verdict()     → Publicar a ocean.agent.decisions.v1
5. await producer.flush()→ Esperar ACK del broker Redpanda (acks=all)
6. consumer.commit()     → ← Sólo aquí se commitea el offset
```

**Idempotencia de debates duplicados:**

El `debate_id` se genera como `uuid5(NAMESPACE_DNS, f"{farm_id}:{alert_event_id}")`. Si la misma alerta se procesa dos veces (por un restart del pod), el segundo debate genera *el mismo* `debate_id`. El mensaje producido a `ocean.agent.decisions.v1` tiene la misma clave Kafka (`farm_id`) y el Consumer downstream puede detectar el duplicado por `debate_id`.

**`max.poll.interval.ms = 60.000 ms`** — Configurado a 60 segundos para acomodar la duración máxima del debate (target P99: 30s) sin que el broker declare al consumer como fallido y reasigne sus particiones.

---

## Alternativas consideradas

| Alternativa | Razón del rechazo |
|-------------|-------------------|
| **Exactly-Once Semantics (EOS)** | Requiere que el Producer del veredicto y el Consumer de alertas compartan el mismo contexto transaccional. LangGraph genera el output de forma asíncrona en un subproceso distinto, haciendo técnicamente compleja esta coordinación sin refactorización significativa. El overhead de latencia (~15-30ms/transacción) tampoco es justificable dado que los debates ya son asincrónicos. |
| **Auto-commit con checkpoint externo (Redis/TimescaleDB)** | Complica el modelo de recuperación: hay que gestionar la consistencia entre el checkpoint externo y el offset de Kafka de forma manual. Introduce una dependencia adicional sin beneficio neto respecto al commit manual. |

---

## Consecuencias

**Positivas:**
- **Cero pérdida de alertas regulatorias.** Un crash del pod en cualquier punto del debate provoca un reinicio limpio del ciclo al recuperarse el consumer.
- Código simple: el commit es una sola línea de Python tras el `flush()` del producer.

**Negativas / trade-offs:**
- En el peor caso (crash justo antes del commit), el debate se ejecuta dos veces. Esto consume tokens LLM dobles para ese evento.
- Los consumers downstream de `ocean.agent.decisions.v1` deben idempotentemente manejar un veredicto duplicado (filtrando por `debate_id` ya procesado).

---

## Estado de implementación

- `enable.auto.commit: False` ya configurado en el Consumer Group `cg-alert-processor` (documentado en `docs/kafka-topics.md`).
- La secuencia de commit está esbozada en `docs/agents-orchestration.md §1`.
- Implementación pendiente en `src/agents/graph.py`.
