# ADR-004 — Reducción de Dimensiones a 768 para Compatibilidad con Ollama

| Campo       | Valor                                          |
|-------------|------------------------------------------------|
| **ID**      | ADR-004                                        |
| **Título**  | Uso de embeddings de 768 dimensiones para el modelo de fallback local (Ollama / nomic-embed-text) |
| **Estado**  | Aprobado                                       |
| **Fecha**   | 2026-03-03                                     |
| **Autores** | Cloud Architecture Team                        |
| **Depende de** | ADR-002 (Circuit Breaker)                   |

---

## Contexto

El pipeline de embeddings de OceanTrust AI usa dos modelos según el estado del Circuit Breaker (ADR-002):

| Estado CB | Modelo | Dimensiones | Contexto |
|-----------|--------|-------------|---------|
| `CLOSED` | `text-embedding-3-large` (OpenAI) | **3072** | Producción — máxima calidad semántica |
| `OPEN` | `nomic-embed-text` (Ollama local) | **768** | Fallback — inferencia local sin dependencia externa |

Qdrant impone una restricción de diseño fundamental: **una colección tiene dimensión fija**. Todos los vectores en una colección deben tener exactamente el mismo número de dimensiones. No es posible mezclar vectores de 3072 y 768 dimensiones en la misma colección sin corromper el índice HNSW.

Existen tres estrategias posibles para reconciliar esta incompatibilidad:

1. **Reducción de dimensiones (`dimensions` parameter):** OpenAI permite solicitar `text-embedding-3-large` con `dimensions=768`. El modelo produce internamente un embedding de 3072 dims y aplica una capa de proyección lineal (Matryoshka Representation Learning) para reducirlo a 768, manteniendo la mayor parte de la representación semántica.
2. **Colección separada para el fallback:** Los vectores de Ollama (768 dims) se escriben en `telemetry_vectors_fallback` y los de OpenAI en `telemetry_vectors` (3072 dims).
3. **Padding con ceros:** Rellenar el vector de 768 dims hasta 3072 con ceros. Técnicamente incorrecto — distorsiona las métricas de distancia coseno.

---

## Decisión

Se adopta **la estrategia combinada de colección separada + reducción de dimensiones selectiva**:

### Estrategia A — Colección de fallback separada (telemetría en tiempo real)

Para el **Vector Ingestion Worker** (telemetría IoT), los vectores producidos por Ollama durante un estado `OPEN` del Circuit Breaker se escriben en la colección `telemetry_vectors_fallback` (dim=768). Esta separación es transparente: los agentes consultan primero `telemetry_vectors` y, si la búsqueda retorna menos de 2 resultados (umbral mínimo, ver `docs/knowledge-ingestion.md §8.3`), amplían la búsqueda a `telemetry_vectors_fallback`.

### Estrategia B — Reducción de dimensiones con OpenAI MRL (corpus documental)

Para las colecciones de documentación regulatoria (`fishing_regulations`, `biological_manuals`, `scientific_papers`), el entorno de **desarrollo local** usa `text-embedding-3-large` con el parámetro `dimensions=768`. Esto permite que los desarrolladores indexen y prueben el pipeline RAG sin necesidad de Ollama, usando colecciones de desarrollo prefijadas `dev_*` (dim=768), y sin duplicar los costos de embedding.

```python
# Producción: dim=3072 (colecciones canónicas)
embed_resp = await client.embeddings.create(
    model="text-embedding-3-large",
    input=text,
    # Sin parámetro dimensions → output: 3072 dims
)

# Dev local / Fallback Ollama: dim=768
embed_resp = await client.embeddings.create(
    model="text-embedding-3-large",
    input=text,
    dimensions=768,   # Matryoshka Representation Learning projection
)
```

La selección entre ambos perfiles se controla mediante la variable de entorno `EMBEDDING_DIM` (declarada en `.env.example`).

---

## Base técnica — Matryoshka Representation Learning (MRL)

OpenAI entrenó `text-embedding-3-large` con la técnica MRL, que optimiza el modelo para que los primeros N dimensiones de cualquier embedding sean un subespacio semánticamente coherente. Esto significa que un vector de 768 dims generado con `dimensions=768` no es simplemente el vector de 3072 dims truncado — es una proyección lineal optimizada que maximiza la calidad semántica en ese subespacio.

**Comparativa de calidad en benchmarks MTEB (Massive Text Embedding Benchmark):**

| Modelo | Dims | MTEB Score (avg) | Costo relativo |
|--------|------|-----------------|----------------|
| `text-embedding-3-large` | 3072 | 64.6 | 1x |
| `text-embedding-3-large` (MRL) | 768 | 62.3 | 1x (menor costo de almacenamiento) |
| `nomic-embed-text` (Ollama) | 768 | 53.1 | 0x (local) |

La degradación de calidad del MRL (3072→768) es de ~2.3 puntos MTEB vs. ~11.5 puntos para Ollama. Para el caso de uso de fallback durante ventanas de outage (<30 min), esta degradación es aceptable.

---

## Alternativas consideradas

| Alternativa | Razón del rechazo |
|-------------|-------------------|
| **Padding con ceros (768→3072)** | Matemáticamente incorrecto. La distancia coseno entre un vector real de 3072 dims y uno con padding es aproximadamente la distancia entre el vector original y el subespacio de 768 dims, sesgando todos los rankings de similaridad. |
| **Una sola colección de 768 dims para todo** | Degradaría permanentemente la calidad semántica de producción. `text-embedding-3-large` a 3072 dims tiene ~11.5 puntos MTEB más que `nomic-embed-text`. Para el Agente Biólogo que cita legislación pesquera, esta diferencia puede traducirse en recuperar el artículo correcto o uno relacionado pero inexacto. |
| **Reindexar la colección en caliente al cambiar de modelo** | Qdrant no soporta modificar la configuración de vectores de una colección existente sin recrearla. Recrear `telemetry_vectors` (100k+ puntos) durante un outage de OpenAI agravaría exactamente el escenario que queremos mitigar. |

---

## Consecuencias

**Positivas:**
- El sistema es resiliente a outages de OpenAI sin pérdida de datos de telemetría.
- El entorno de desarrollo funciona con embeddings de calidad similar a producción sin requerir Ollama (usando MRL a 768 dims contra OpenAI).
- Las colecciones de producción mantienen la máxima calidad semántica (3072 dims).

**Negativas / trade-offs:**
- Los agentes deben ser conscientes de que `telemetry_vectors_fallback` puede existir y contener datos recientes durante un outage. La lógica de búsqueda debe consultar ambas colecciones y unir resultados.
- El equipo de operaciones debe monitorear el ratio de escrituras en `telemetry_vectors_fallback` como señal de degradación del proveedor de embeddings.

---

## Estado de implementación

- Colección `telemetry_vectors_fallback` (dim=768) provisionada en `bin/scripts/provision_qdrant.sh`.
- Variable `EMBEDDING_DIM` declarada en `.env.example`.
- La selección del perfil de embedding está implementada en `src/ingestion/knowledge_loader.py` vía `config/embeddings.py` (referenciado en `docs/knowledge-ingestion.md §4.2`).
