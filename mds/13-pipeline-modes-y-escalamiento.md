# 13 — Modos de pipeline DLT y escalamiento del medallion

> **Audiencia**: ingeniero/arquitecto que necesita entender por qué hoy el flujo
> Mongo→Bronze tarda 10–14 min, qué se puede cambiar para bajarlo, y cómo escala
> cada componente cuando la carga crece.
>
> **Pre-requisito**: haber leído `04-cdc-ingestion.md` y `05-medallion-databricks.md`.

---

## 1. El flujo end-to-end (qué pasa cuando alguien aprueba un expense)

```
┌──────────────┐  oplog stream     ┌──────────────┐  Kafka produce   ┌──────────────┐
│   MongoDB    │ ────────────────► │   Debezium   │ ───────────────► │  MSK (Kafka) │
│  (Atlas)     │   change stream   │ (ECS Fargate)│   SASL/IAM       │  Provisioned │
└──────────────┘   continuo, ms    └──────────────┘   continuo, ms   └──────┬───────┘
                                                                            │
                                                            spark.readStream │ Kafka source
                                                                            ▼
                                                                  ┌──────────────────┐
                                                                  │ DLT Bronze       │
                                                                  │ (5 streaming     │
                                                                  │  tables Kafka)   │
                                                                  └──────┬───────────┘
                                                          dlt.read       │
                                                                         ▼
                                                                  ┌──────────────────┐
                                                                  │ DLT Silver       │
                                                                  │ (4 ST + 1 MV)    │
                                                                  └──────┬───────────┘
                                                                         │
                                                                         ▼
                                                                  ┌──────────────────┐
                                                                  │ DLT Gold         │
                                                                  │ (2 MVs)          │
                                                                  └──────────────────┘
```

### 1.1. MongoDB → Debezium (continuo, latencia ms)

MongoDB Atlas mantiene un **change stream** sobre el oplog (oplog = bitácora
inmutable de cada insert/update/delete). Debezium Server abre una conexión
persistente a ese stream y consume cada evento en cuanto Mongo lo flushea —
típicamente decenas de milisegundos.

Importante: **NO hay un "job que se despierta cada X segundos a revisar Mongo"**.
Es un canal abierto permanentemente, push-based desde Mongo a Debezium.

### 1.2. Debezium → MSK (continuo, latencia ms)

Por cada evento del oplog, Debezium aplica:

- **SMT `ExtractNewDocumentState`** — saca el documento "después del cambio" del
  envelope CDC.
- **`add.fields=op,source.ts_ms`** — agrega dos columnas: `__op` (`c`/`u`/`d`/`r`)
  y `__source_ts_ms` (cuándo Debezium leyó el evento del oplog).
- **Producer Kafka con auth SASL/IAM** — escribe al topic `nexus.nexus_dev.<collection>`.

Debezium produce **un mensaje Kafka por evento del oplog**. No hay batching
agresivo. Por eso, en el ejemplo `exp_01KQ57VFDM5E1XCFBY1Y2RQQ3J` que analizamos:
los 4 cambios en Mongo (`pending → processing → hitl_required → approved`) se
publicaron a Kafka prácticamente al mismo segundo en que ocurrieron en Mongo.

### 1.3. MSK (Kafka) — almacén durable

MSK guarda los mensajes en topics particionados, replicados y retenidos
(default 7 días). Es solo el **buffer durable** entre productor y consumidor —
no procesa nada, no decide cuándo entregar. Los consumidores leen cuando
quieran, desde el offset que quieran.

### 1.4. MSK → DLT Bronze (**aquí está el cuello de botella actual**)

Cada notebook de bronze (`src/bronze/cdc_*.py`) tiene un
`spark.readStream.format("kafka")` suscrito a un topic. **El stream existe,
pero solo corre cuando el pipeline DLT está encendido**.

Hoy `bronze_cdc.yml:12` dice `continuous: false`. Eso significa que el
pipeline está apagado por default y solo se enciende cuando el job
`nexus_cdc_refresh` (cron `0 */10 * * * ?` = cada 10 min en punto) lo
dispara. Cada disparo:

1. Spinnea un cluster classic m5.large (cold start 2–4 min).
2. Lee TODOS los mensajes nuevos desde el último offset commiteado en un
   micro-batch.
3. Escribe a las 5 tablas bronze.
4. Ejecuta silver (depende de bronze).
5. Ejecuta gold (depende de silver).
6. Apaga el cluster.

Resultado: aunque Debezium haya puesto el mensaje en Kafka en el segundo 1,
nadie lo lee hasta que llegue el siguiente cron (peor caso: minuto 9 + 4 min de
cold start = **13 min de espera**).

### 1.5. DLT Silver y Gold

- **Silver**: 4 streaming tables que aplican CDC con `dlt.apply_changes` (SCD1)
  + 1 materialized view (`hitl_discrepancies`).
- **Gold**: 2 materialized views (`expense_audit`, `expense_chunks`).
- **`gold.expense_embeddings`**: NO está en DLT — la escribe el Temporal
  activity `trigger_vector_sync` (INSERT puro contra Delta).

---

## 2. ¿Qué es streaming table vs materialized view en DLT?

Distinguir esto es **crítico** porque define qué se puede pasar a continuous y
qué no.

| Característica | Streaming Table (ST) | Materialized View (MV) |
|---|---|---|
| Decorador típico | `@dlt.table` con `spark.readStream` o `dlt.create_streaming_table` + `apply_changes` | `@dlt.table` con `spark.read` o `dlt.read` |
| Procesamiento | Solo procesa **datos nuevos** (incremental, append-only o CDC) | Recomputa **todo el resultado** cada vez que el upstream cambia |
| Source típico | Kafka, Auto Loader, otra ST | Cualquier tabla Delta, joins, agregaciones |
| Soporta `apply_changes` | ✅ Sí (es para SCD1/SCD2) | ❌ No |
| Estado entre runs | Mantiene checkpoints (offsets Kafka, watermarks) | Sin estado — cada run es un re-batch |
| Continuous mode | ✅ Procesa segundo a segundo | ✅ Recomputa cuando upstream cambia (batch reactivo, no streaming) |
| Costo en continuous | Cluster siempre encendido leyendo el stream | Depende: si upstream cambia poco, casi 0; si cambia mucho, recompute frecuente |

### 2.1. La regla de oro: no se puede `readStream` sobre una tabla SCD1

Esta es la razón por la que silver→gold y gold internamente NO son streams.

`silver.expenses` usa `dlt.apply_changes(stored_as_scd_type="1")` — eso significa
que Delta hace `MERGE` sobre la tabla, lo que produce **commits con DELETEs y
UPDATEs**, no solo INSERTs. Si tratas de hacer `spark.readStream.table("silver.expenses")`
en un pipeline downstream, falla con:

```
DELTA_SOURCE_TABLE_IGNORE_CHANGES: Detected a non-additive operation in the source.
```

`skipChangeCommits=true` no sirve porque justo el evento que necesitas (el
update a `status = 'approved'`) es un commit que se saltaría.

**Por eso `gold.expense_audit` es MV** (lee con `spark.read`, no `readStream`).
Está documentado en `src/gold/expense_audit.py:18-24`:

> "este nodo es un materialized view (no streaming) porque su fuente
> `silver.expenses` se escribe con `apply_changes` (MERGE/SCD1). Un streaming
> source de Delta falla con `DELTA_SOURCE_TABLE_IGNORE_CHANGES` al detectar
> updates — lo comprobamos en prod en abril 2026: 5 updates seguidos del
> pipeline gold fallaron con ese mismo error."

### 2.2. La excepción especial: `gold.expense_embeddings` está fuera de DLT

`gold.expense_chunks` es MV. Sobre un MV no se permite UPDATE/MERGE (DLT lo
prohíbe). Pero los embeddings necesitan poder reescribirse. Solución: la
tabla está **fuera del pipeline DLT**, manejada manualmente por el Temporal
activity `trigger_vector_sync`. Append-only INSERT + dedupe en lectura
(`ROW_NUMBER() ORDER BY updated_at DESC`).

Ver `mds/12-delta-merge-concurrencia-y-parquet.md` para el racional completo.

---

## 3. Inventario actual del pipeline (modos y dependencias)

| Capa | Tabla | Tipo | Pipeline | Modo | Compute |
|---|---|---|---|---|---|
| **bronze** | `mongodb_cdc_expenses` | ST (Kafka) | `bronze_cdc_pipeline` | triggered (10 min) | classic m5.large |
| bronze | `mongodb_cdc_receipts` | ST (Kafka) | mismo | mismo | mismo |
| bronze | `mongodb_cdc_hitl_tasks` | ST (Kafka) | mismo | mismo | mismo |
| bronze | `mongodb_cdc_ocr_extractions` | ST (Kafka) | mismo | mismo | mismo |
| bronze | `mongodb_cdc_expense_events` | ST (Kafka) | mismo | mismo | mismo |
| **silver** | `expenses` | ST (apply_changes SCD1) | `silver_pipeline` | triggered (10 min) | serverless |
| silver | `ocr_extractions` | ST (apply_changes SCD1) | mismo | mismo | mismo |
| silver | `expense_events` | ST (apply_changes SCD1) | mismo | mismo | mismo |
| silver | `hitl_events` | ST (apply_changes SCD1) | mismo | mismo | mismo |
| silver | `hitl_discrepancies` | **MV** (lee de `hitl_events`) | mismo | mismo | mismo |
| **gold** | `expense_audit` | **MV** (join 4 silver) | `gold_pipeline` | triggered (10 min) | serverless |
| gold | `expense_chunks` | **MV** (lee `expense_audit`) | mismo | mismo | mismo |
| gold | `expense_embeddings` | **fuera de DLT** | n/a | activity Temporal | n/a |

El job `nexus_cdc_refresh` orquesta los 3 pipelines secuencialmente: `bronze
→ silver → gold`. Cron `0 */10 * * * ?`.

---

## 4. Modo triggered vs continuous — qué cambia

### 4.1. Triggered (modo actual)

- Pipeline está apagado salvo cuando el job lo dispara.
- Cluster se enciende, procesa lo nuevo en un micro-batch, se apaga.
- Costos: pagas solo el tiempo que el cluster está vivo.
- Latencia: `cron interval + cold start + processing time`.
- Para nuestro caso: 10 min + 2–4 min + ~30s = **~12–14 min worst case**.

### 4.2. Continuous

- Pipeline está siempre vivo.
- Para streaming tables: el `readStream` consume mensajes nuevos de Kafka
  segundo a segundo (default `processingTime=500ms`).
- Para materialized views: cada vez que su upstream Delta tiene un commit
  nuevo, el motor DLT detecta el cambio y dispara un recompute incremental
  del MV. NO es streaming — es **batch reactivo más frecuente**.
- Costo: cluster encendido 24/7 (classic) o consumo continuo de DBUs
  (serverless).
- Latencia: segundos para STs, minuto o menos para MVs.

### 4.3. La cita oficial sobre coexistencia

De la doc oficial de Lakeflow Declarative Pipelines:

> "Pipeline mode is independent of the type of table being computed. Both
> materialized views and streaming tables can be updated in either pipeline
> mode."

Es decir, **un pipeline continuous SÍ puede contener MVs**. Esto contradice un
mito común. Lo que el modo continuous NO puede hacer es convertir un MV en
streaming — el MV sigue siendo batch, pero se dispara más seguido.

### 4.4. Restricción serverless + continuous

Caveat importante:

> "Standard performance mode is supported for triggered pipeline mode only.
> Continuous execution is not supported. To use standard performance mode in
> continuous pipelines, reach out to your Databricks account team."

Hoy `silver.yml` y `gold.yml` están con `serverless: true`. Pasarlos a
`continuous: true` tal cual va a fallar el deploy. Habría que migrarlos a
classic compute o pedir performance-optimized mode al account team de
Databricks.

`bronze_cdc.yml` ya está con `serverless: false` (classic), así que NO tiene
esa restricción — se puede pasar a continuous directamente.

---

## 5. Escalamiento — cómo crece cada componente con la carga

Esta sección responde directamente: "¿qué pasa cuando hay millones de
transacciones?".

### 5.1. MongoDB

- **Lo que escala**: writes, reads, oplog throughput.
- **Cómo**: Atlas tiers (M10, M20, M30…) — más vCPU, más RAM, más IOPS.
- **Multi-shard** si superas un single-replica-set.
- **El oplog** es un capped collection (default 5% del disco). Si el throughput
  es muy alto y el oplog se rota antes de que Debezium lo lea, se pierden
  eventos. Atlas alerta y permite agrandarlo.
- **Cuello típico**: writes/sec del primary + IOPS del disco.

### 5.2. Debezium Server

- **Lo que es**: una JVM single-instance que mantiene el cursor del change
  stream. **NO se escala horizontalmente como un grupo de workers** — solo
  hay un consumidor por database.
- **Cómo escala vertical**: subir CPU/memoria del task ECS. Hoy: `cpu=1024,
  memory=2048` en `infra/terraform/debezium.tf`.
- **Cómo escala horizontal**: NO se puede paralelizar por collection en el
  mismo Debezium Server (un solo cursor MongoDB). Sí se puede correr
  **múltiples Debezium Servers**, cada uno con `collection.include.list`
  distinto, escribiendo a topics distintos.
- **Throughput típico**: una instancia Debezium con 1 vCPU maneja
  10k–50k eventos/s para MongoDB simple. Si superas eso, partir collections
  en otro Debezium.
- **Cuello típico**: serialización JSON + producer Kafka. Si Kafka está
  lento, Debezium aplica back-pressure leyendo más despacio del oplog.

### 5.3. MSK / Kafka

- **Lo que almacena**: cada topic se divide en N **partitions**. Cada partition
  es una secuencia ordenada de mensajes, replicada en M brokers.
- **Cómo escala writes**: más partitions por topic → más productores en paralelo.
  Pero el orden está garantizado **solo dentro de una partition**. Si necesitas
  orden global por `expense_id`, particiona por `expense_id` y un consumidor por
  partition.
- **Cómo escala reads**: **un consumer group con N consumers** lee N partitions
  en paralelo (cada consumer toma 1+ partitions). Si tienes 3 partitions y 5
  consumers, solo 3 trabajan; los otros 2 quedan idle.
- **Cómo escala el cluster**: en Provisioned (lo que usamos hoy), agregar más
  brokers (`number_of_broker_nodes`) o subir el `instance_type`. Hoy:
  `t3.small` con 3 brokers (uno por AZ).
  - Una broker `t3.small` aguanta ~1 MB/s de ingest sostenido.
  - Una broker `m5.large` aguanta ~10 MB/s.
  - Una broker `m5.4xlarge` aguanta ~100 MB/s.
- **MSK Serverless** (que tenemos también disponible): escala automáticamente,
  pagas por GB ingerido. Buena opción si el tráfico es muy variable.
- **Configuración actual**:
  - `default.replication.factor=2` (un broker puede caer)
  - `num.partitions=3` (default por topic)
  - 5 topics activos (uno por collection)
- **Cuello típico**: throughput de red del broker, replication lag.

### 5.4. DLT Bronze (la capa más cercana a Kafka)

Dos dimensiones de escalamiento independientes:

**(a) Throughput por stream**

Cada notebook bronze tiene su propio `readStream` Kafka. Si un topic empieza a
recibir mucho tráfico, **el throughput está limitado por las partitions**
(Spark asigna 1 task por partition Kafka). Si tienes 3 partitions y 1 worker
con 4 cores, los 4 cores procesan máximo 3 partitions en paralelo.

**Solución**: aumentar partitions del topic + agregar más workers al cluster
DLT. Hoy bronze tiene `autoscale: min_workers=1, max_workers=2` —
escalable a más.

**(b) Throughput agregado de la capa**

Hay 5 streams independientes en el mismo pipeline. Cada uno consume CPU y
memoria. El cluster m5.large (2 vCPU / 8 GB) es **suficiente para 5 streams
de bajo volumen en dev**, como dice el comment en `bronze_cdc.yml:18-19`.

Si una collection (digamos `expenses`) crece 10×, hay que:
1. Subir partitions del topic Kafka (`alter-topic --partitions 10`).
2. Subir `max_workers` del cluster DLT (`autoscale: max_workers: 8`).
3. Posiblemente cambiar `node_type_id` a algo más grande (`m5.xlarge`).

**Cuello típico**: I/O de red al leer Kafka + escritura a Delta (S3).

### 5.5. DLT Silver

Las streaming tables de silver hacen `apply_changes` con `sequence_by`. Spark
necesita ordenar dentro de cada micro-batch antes de hacer MERGE. Si hay
muchos cambios concurrentes:

- El sort consume memoria.
- El MERGE escribe parquet files nuevos.
- Si las escrituras concurrentes tocan los mismos archivos parquet, hay
  retries por `DELTA_CONCURRENT_APPEND` (ver `mds/12-delta-merge-concurrencia-y-parquet.md`).

**Cómo escala**: serverless (que es lo que tenemos) escala automáticamente
agregando workers. En classic: subir `max_workers` y/o el tipo de nodo.

Si la tasa de updates por `expense_id` es muy alta (por ejemplo, agregamos
campos que cambian mucho), conviene **particionar por `tenant_id`** para que
cada tenant trabaje sobre archivos parquet distintos y no haya contención.

**Cuello típico**: el MERGE en Delta — sort + write + commit serializado.

### 5.6. DLT Gold (MVs)

Cada MV recompute = una query batch que lee silver completo + escribe gold.
No hay estado incremental por defecto. Si silver crece a millones de filas,
el recompute crece linealmente.

**Cómo escala**:
1. Más compute (workers/serverless).
2. **Optimizar el recompute** activando `enableMaterializedViewIncrementalRefresh`
   (feature de Databricks que detecta qué filas cambiaron y solo recomputa
   esas — disponible para algunos patrones de query, no todos).
3. **Particionar y clusterizar** la tabla gold (ya tenemos
   `cluster_by=["tenant_id", "final_date"]` en `expense_audit` y
   `partition_cols=["tenant_id"]` en `expense_chunks`).

**Cuello típico**: tiempo de recompute completo. Para 1M de expenses puede
tardar minutos. Para 100M, horas. Ahí se justifica refactorizar a streaming
table con apply_changes (lo que evitamos hoy por la razón documentada).

### 5.7. `gold.expense_embeddings` (fuera de DLT)

Append-only desde N activities Temporal en paralelo. Cada activity hace 1
INSERT (1 archivo parquet pequeño nuevo).

**Cómo escala**: trivialmente. INSERTs concurrentes no se pisan en Delta
(cada writer agrega archivos nuevos, no toca existentes). Hoy ya hay retry
contra `DELTA_CONCURRENT_APPEND` por si hay un OPTIMIZE corriendo en
paralelo.

**El tradeoff**: muchos INSERTs producen muchos archivos parquet pequeños
("small files problem"). Mitigación: `OPTIMIZE` periódico (semanal) +
dedupe via `ROW_NUMBER` en lectura.

**Cuello típico**: small files si no hay OPTIMIZE.

### 5.8. Resumen del escalamiento

| Componente | Escala vertical | Escala horizontal | Cuello típico |
|---|---|---|---|
| MongoDB | Atlas tier (M10→M30→M60) | Sharding | writes/sec primary, oplog rotation |
| Debezium | CPU/memoria del task | Múltiples instancias por collection | producer Kafka, JSON serializer |
| MSK | Broker `instance_type` | Más brokers + más partitions | throughput de red |
| DLT Bronze | Worker type | `max_workers` + partitions Kafka | I/O Kafka + Delta write |
| DLT Silver | Serverless auto / max_workers | Particionar por tenant | MERGE serialization |
| DLT Gold | Serverless auto / max_workers | Refactor a apply_changes | recompute completo |
| Embeddings | n/a | INSERTs paralelos triviales | small files |

---

## 6. Concurrencia con múltiples workflows simultáneos

Pregunta clave: si tenemos 100 workflows aprobando expenses al mismo tiempo,
¿se pisan? Repasemos cada punto de potencial colisión:

| Punto | Riesgo de colisión | Mitigación |
|---|---|---|
| 100 workflows escriben a Mongo | NULO | Mongo serializa writes por documento; el oplog garantiza orden por collection |
| 100 mensajes en Kafka topic | NULO | Append a partitions; orden garantizado por partition |
| Bronze ingesta 100 mensajes en un micro-batch | NULO | Append-only; cada mensaje una fila |
| Silver `apply_changes` con 100 updates concurrentes | NULO | `sequence_by=__source_ts_ms` ordena; el último gana |
| Gold MV recompute mientras llegan más cambios | NULO | El recompute trabaja sobre snapshot consistente; el siguiente trigger captura los cambios pendientes |
| 100 activities `trigger_vector_sync` insertan a `gold.expense_embeddings` | BAJO | Append-only INSERT (no MERGE) → no chocan; retry contra `DELTA_CONCURRENT_APPEND` por si OPTIMIZE corre en paralelo |
| Job `cdc_refresh` solapándose | NULO | `max_concurrent_runs: 1` |

**Conclusión**: el modelo actual ya está blindado contra concurrencia. Ningún
cambio a continuous afecta estos invariantes.

---

## 7. Plan recomendado para reducir latencia (sin romper lo que funciona)

### 7.1. Por qué NO podemos hacer "todo continuous streaming"

Recapitulando:
1. `silver.expenses` y demás silver-STs usan `apply_changes` (SCD1/MERGE).
2. `gold.expense_audit` y `gold.expense_chunks` son MVs porque no se puede
   `readStream` sobre tablas SCD1 (falla con `DELTA_SOURCE_TABLE_IGNORE_CHANGES`).
3. Convertir gold en streaming exigiría reescribir las MVs como
   apply_changes con propagación de cambios a través de Delta change feed —
   semanas de trabajo y la decisión documentada en abril 2026 fue NO hacerlo.
4. Silver y gold están en `serverless: true`. Pasarlos a `continuous: true`
   tal cual va a fallar el deploy (limitación serverless documentada).

### 7.2. La opción híbrida que sí podemos hacer

**Tres cambios pequeños, alto impacto:**

| # | Cambio | Archivo | Línea | Riesgo |
|---|---|---|---|---|
| 1 | `continuous: false` → `continuous: true` | `nexus-medallion/resources/pipelines/bronze_cdc.yml` | 12 | BAJO |
| 2 | Quitar el task `bronze_cdc` del job (ya no lo necesita) | `nexus-medallion/resources/jobs/cdc_refresh.yml` | 17–20 | NULO |
| 3 | Cron `0 */10 * * * ?` → `0 * * * * ?` (cada 1 min) | `nexus-medallion/resources/jobs/cdc_refresh.yml` | 12 | BAJO |

**Resultado esperado**:

| Métrica | Hoy | Después |
|---|---|---|
| Latencia Mongo→Bronze | 10–14 min | <5s |
| Latencia Mongo→Silver | 10–14 min | ~1 min |
| Latencia Mongo→Gold | 10–14 min | ~2 min |
| Latencia Mongo→Embedding (lo que ve el RAG) | 12–18 min | ~3 min |
| Costo bronze cluster | ~$20/mes (3min × 6/hr × $0.096/hr) | ~$70/mes (24/7 m5.large) |
| Costo silver+gold serverless | bajo (6 runs/hr) | medio (60 runs/hr) |

### 7.3. Por qué este plan es seguro

- No toca código de tablas (ni schemas, ni transformaciones).
- No toca el modelo de concurrencia (ya validado).
- No requiere refactor de MVs.
- No requiere cambiar serverless→classic en silver/gold.
- Es **trivialmente reversible**: revertir 3 líneas y vuelves a triggered.

### 7.4. Si después necesitamos aún menos latencia

- **Bajar silver/gold a continuous** → requiere migrar a classic compute o
  pedir performance-optimized serverless al account team. Latencia <1 min total.
- **Refactorizar gold a apply_changes streaming** → requiere reescribir
  `expense_audit` y `expense_chunks`. Latencia segundos. Alto esfuerzo.
- **Particionar Kafka topics más agresivamente** + más workers DLT → necesario
  solo cuando el throughput supere ~1k expenses/s.

---

## 8. Preguntas frecuentes

**P: ¿Por qué el `_cdc_ingestion_ts` de los 4 cambios del expense
`exp_01KQ57VFDM5E1XCFBY1Y2RQQ3J` era idéntico (16:01:38.590) si Debezium los
publicó con segundos de diferencia?**

R: Porque `_cdc_ingestion_ts` se calcula con `current_timestamp()` dentro del
notebook bronze, en el momento del micro-batch. Como el bronze corre triggered
cada 10 min, los 4 mensajes (que estaban esperando en Kafka desde minutos
antes) se procesaron juntos en el mismo micro-batch a las 16:01:38.

**P: ¿Hay algún job que "active" a Debezium cuando hay cambios?**

R: NO. Debezium tiene una conexión TCP persistente al change stream de Mongo.
Mongo le hace push de cada cambio del oplog en tiempo real, no hay polling
intermedio. Lo que sí es polling es Kafka→Bronze (cuando el pipeline está
triggered).

**P: ¿Qué pasa si Debezium se cae?**

R: Hoy guarda offsets en `MemoryOffsetBackingStore` (memoria volátil). Cada
restart re-snapshotea las 5 collections desde cero (~5s para volúmenes de dev).
Para prod: cambiar a `KafkaOffsetBackingStore` que persiste offsets en Kafka
(comment lo menciona en `infra/terraform/debezium.tf`).

**P: ¿Qué pasa si MSK pierde un broker?**

R: `replication_factor=2` significa que cada partition vive en 2 brokers. Si
uno cae, el otro sigue sirviendo. Si caen los 2: data loss para esa partition.
Para mayor durabilidad: `replication_factor=3` y `min.insync.replicas=2`.

**P: ¿Por qué el activity `trigger_vector_sync` poole gold.expense_chunks 18
veces si bronze ya tiene los datos?**

R: Porque entre bronze y gold hay 2 capas más (silver MERGE + gold MV
recompute). El activity espera específicamente a que el chunk aparezca en gold
para poder embeberlo. Si pasamos a el plan híbrido (sección 7), las 18
iteraciones se reducen a 2–3.

**P: ¿El plan híbrido funciona si tengo 100 workflows aprobando
simultáneamente?**

R: Sí. Bronze continuous procesa los 100 mensajes en stream (subsegundo).
Silver con apply_changes los ordena por timestamp. Gold MV recomputa los
nuevos. `trigger_vector_sync` inserta 100 embeddings concurrentes
(append-only, no chocan). Ver tabla de concurrencia en sección 6.

---

## 9. Referencias

- Doc oficial: [Triggered vs. continuous pipeline mode](https://docs.databricks.com/aws/en/ldp/pipeline-mode)
- Doc oficial: [Configure a serverless pipeline](https://docs.databricks.com/aws/en/ldp/serverless)
- Doc oficial: [Lakeflow Spark Declarative Pipelines concepts](https://docs.databricks.com/aws/en/ldp/concepts)
- Doc oficial: [Materialized views in Lakeflow](https://docs.databricks.com/aws/en/ldp/materialized-views)
- Doc oficial: [Serverless compute limitations](https://docs.databricks.com/aws/en/compute/serverless/limitations)
- Repo interno: `mds/04-cdc-ingestion.md` — detalle del SMT Debezium y schema bronze.
- Repo interno: `mds/05-medallion-databricks.md` — diseño del medallion.
- Repo interno: `mds/12-delta-merge-concurrencia-y-parquet.md` — por qué embeddings es append-only.
- Repo interno: `mds/11-incidente-hitl-no-llega-a-gold.md` — incidente que motivó el diseño actual.
