# 12 · Por qué dos `MERGE` simultáneos chocan en Delta — y cómo lo arreglamos

> **Audiencia**: tú (para entenderlo y poderlo explicar). Lectura ~15 min.
>
> **Contexto**: el día que aprobaste varios HITL en paralelo, uno crasheó
> con `DELTA_CONCURRENT_APPEND.ROW_LEVEL_CHANGES` al hacer `MERGE` en
> `nexus_dev.gold.expense_embeddings`. Tu intuición ("son filas distintas,
> ¿por qué chocan?") es correcta a nivel lógico. Acá explicamos por qué
> Delta lo ve distinto, qué son los archivos parquet, cómo funciona el
> control de concurrencia, qué intentamos primero, qué descubrimos al
> investigar, y por qué la solución final fue **append-only + dedupe en
> lectura** (no row-level concurrency).

---

## 1. La pregunta correcta

Si dos HITL aprueban dos expenses distintos, cada uno escribe una fila
distinta (`chunk_id` distinto). Lógicamente son operaciones disjuntas.
¿Por qué Delta las trata como conflictivas?

Respuesta corta: **Delta es un motor transaccional sobre archivos
parquet inmutables, no una base de datos fila-a-fila**. Su control de
concurrencia "por defecto" trabaja a nivel **archivo**, no a nivel fila.
Dos transacciones que toquen el mismo archivo parquet entran en conflicto
aunque modifiquen filas distintas. Y peor: cuando ambas transacciones
**insertan** filas nuevas en la misma partición, ni siquiera la
"row-level concurrency" del DBR moderno las salva — esa feature sólo
resuelve UPDATE/DELETE/MERGE-WHEN-MATCHED, no APPEND.

Para entender el "por qué" hay que entender 3 piezas: qué es un parquet,
cómo Delta organiza la tabla con un transaction log, y cómo decide cuándo
hay conflicto.

---

## 2. Qué es un archivo `.parquet` (y por qué importa)

### 2.1 Inmutabilidad

Un parquet es un archivo **columnar, comprimido e inmutable**. Una vez
escrito, no se modifica nunca: no se puede "actualizar la fila 7". Si
quieres cambiar una fila, escribes un parquet **nuevo** con la versión
actualizada y, lógicamente, "ocultas" el anterior.

```
gold/expense_embeddings/
  tenant_id=t_alpha/
    part-00000-abc123.parquet      ← 1.000 filas, escrito ayer
    part-00001-def456.parquet      ← 1.000 filas, escrito hoy 03:41
    part-00002-ghi789.parquet      ← 1.000 filas, escrito hoy 03:42
```

Cada parquet trae **estadísticas de min/max** por columna en su footer.
Para nuestra tabla:

```
part-00001-def456.parquet stats:
  chunk_id  min="chk_01KQ3JYZ..." max="chk_01KQ3K2A..."
  tenant_id min="t_alpha"          max="t_alpha"
```

Esas stats son críticas: cuando alguien hace `SELECT ... WHERE chunk_id =
'chk_01KQ3K00...'`, Delta abre **sólo** los parquets cuyo rango min/max
contiene ese `chunk_id`. Es el mecanismo de **data skipping**.

### 2.2 La tabla = log + parquets

Una tabla Delta es:

```
expense_embeddings/
  _delta_log/
    00000000000000000000.json     ← v0: CREATE TABLE
    00000000000000000001.json     ← v1: INSERT  (añadió part-00000)
    00000000000000000002.json     ← v2: MERGE   (añadió part-00001, marcó part-00000 como removido)
    00000000000000000003.json     ← v3: MERGE   (añadió part-00002, marcó part-00001 como removido)
    ...
  tenant_id=t_alpha/
    part-00000-abc123.parquet     ← "removido" en v2 (el log lo dice; el archivo sigue en S3 hasta VACUUM)
    part-00001-def456.parquet     ← "removido" en v3
    part-00002-ghi789.parquet     ← vigente
```

El `_delta_log/` es la **fuente de verdad**. Cada commit es un JSON con
"qué archivos se añaden, qué archivos se quitan, en qué versión queda la
tabla". Leer la tabla = leer el log + leer sólo los parquets vigentes.

> **Mental model**: piensa en Git. Los parquets son blobs (objetos
> inmutables); el `_delta_log/` es la cadena de commits. Una "fila" es
> sólo una línea dentro de un blob. No hay forma de editar la línea sin
> reescribir el blob entero.

---

## 3. Qué hace `MERGE` por dentro

Cuando ejecutas:

```sql
MERGE INTO gold.expense_embeddings t
USING (...) s
ON t.chunk_id = s.chunk_id AND t.tenant_id = s.tenant_id
WHEN MATCHED  THEN UPDATE SET embedding = s.embedding
WHEN NOT MATCHED THEN INSERT (...)
```

Delta no edita ninguna fila. Hace esto:

1. **Plan**: lee las stats de los parquets vigentes y elige cuáles
   "podrían contener" el `chunk_id` que matcheás. Llamémoslos
   `[part-00001, part-00002]`.
2. **Re-escritura**: lee TODAS las filas de esos parquets, aplica la
   lógica de MERGE en memoria (algunas filas cambian, otras no), y
   escribe parquets nuevos `[part-00003, part-00004]` con el resultado.
3. **Commit**: intenta escribir un commit nuevo en `_delta_log/`:

   ```
   v4: { add: [part-00003, part-00004], remove: [part-00001, part-00002] }
   ```

Una sola fila de cambio puede implicar reescribir 1.000 filas. Es el
costo de la inmutabilidad de parquet.

---

## 4. El conflicto: dos MERGE simultáneos

Imaginá la timeline de 2 HITL aprobando casi al mismo tiempo:

```
t=0    HITL_A lee log: tabla en v3. Archivos vigentes: [p1, p2].
t=0    HITL_B lee log: tabla en v3. Archivos vigentes: [p1, p2].
t=1    HITL_A planea: "voy a tocar p1" (porque su chunk_id_A está en min/max de p1).
t=1    HITL_B planea: "voy a tocar p1" (su chunk_id_B también cae en min/max de p1, aunque su FILA no exista todavía).
t=2    HITL_A escribe p3 = re-escritura de p1 + su fila nueva.
t=2    HITL_B escribe p4 = re-escritura de p1 + su fila nueva.
t=3    HITL_A intenta commitear v4 = { add: [p3], remove: [p1] }.   ✓ commit gana.
t=3    HITL_B intenta commitear v4 = { add: [p4], remove: [p1] }.   ✗ "v4 ya existe".
       Delta reintenta internamente: lee v4, ve que A removió p1.
       p4 referencia un p1 que ya no existe. Delta aborta:
       DELTA_CONCURRENT_APPEND — "another transaction modified the same files".
```

**Las filas eran distintas**. Pero los dos planeadores eligieron el
**mismo archivo `p1`** porque ambos `chunk_id` caían dentro del rango
min/max de `p1`, y porque tenant_id=t_alpha cae en la misma partición y
hay pocos archivos. Al re-escribir el archivo entero, se pisaron.

Esto es **optimistic concurrency control a nivel archivo**: Delta deja
que las transacciones corran en paralelo, y al hacer commit verifica si
el conjunto de archivos que removías sigue intacto. Si no, una aborta.

> **Por qué `.ROW_LEVEL_CHANGES`** en el error: indica que Databricks
> ya intentó usar row-level concurrency (Deletion Vectors + Row
> Tracking) y aún así detectó conflicto. Más sobre esto en §6.

---

## 5. Por qué partir en N tablas NO es la respuesta

Tu intuición fue: "¿por qué no una tabla por expense, así nunca chocan?".
Razones para no hacerlo:

- **Cardinalidad**: cada tenant genera miles de expenses → miles de
  tablas. Unity Catalog no escala bien a eso (overhead de metadata, IAM,
  tags, lineage).
- **Queries cross-expense**: `vector_search.py` necesita un único `LEFT
  JOIN` contra todos los embeddings del tenant. Con N tablas tendrías
  que materializar una vista UNION ALL gigante o emitir N queries.
- **Costo de mantenimiento**: cada tabla Delta tiene su propio
  `_delta_log`, costo fijo de OPTIMIZE/VACUUM, etc.
- **No resuelve el caso real**: si un tenant tiene mucho tráfico (varios
  HITL por minuto), incluso "una tabla por expense" tendría conflictos
  cuando ese expense corre re-vectorizaciones concurrentes.

La forma idiomática de Delta es **una tabla, muchas particiones, y un
patrón de escritura que evite contención**.

---

## 6. Lo que probamos primero (y por qué NO funcionó): Row-Level Concurrency

Desde DBR 14, Delta tiene **row-level concurrency**: dos features
combinadas, **Deletion Vectors** + **Row Tracking**, que permiten
resolver conflictos a nivel fila para `UPDATE`/`DELETE`/`MERGE`.

### 6.1 Deletion Vectors (DV)

En vez de re-escribir el parquet entero cuando una fila cambia, Delta
escribe un archivo lateral pequeño (`*.bin`) que dice "en
`part-00001-def456.parquet`, las filas con índices [42, 87] están
borradas". Lectores modernos aplican el DV transparentemente.

### 6.2 Row Tracking

Asigna un `_metadata.row_id` estable a cada fila. Permite que el commit
de Delta diga "modifiqué row_id=12345" en vez de "modifiqué
part-00001". Dos transacciones que tocan row_ids distintos del mismo
archivo no chocan.

### 6.3 La trampa: NO cubre INSERT-on-same-partition

Cuando investigamos nuestra tabla:

```
DESCRIBE DETAIL nexus_dev.gold.expense_embeddings
  → properties: {
      "delta.enableDeletionVectors": "true",
      "delta.enableRowTracking":     "true",
      ...
    }
```

**La tabla ya las tenía activas** (Databricks Serverless las activa por
default en DBR 15+). Y aún así el conflicto ocurría. La razón está en
la letra chica de la doc de Databricks:

> Row-level concurrency resuelve conflictos entre operaciones que
> **modifican filas existentes** (UPDATE/DELETE/MERGE WHEN MATCHED).
> **NO** resuelve conflictos cuando dos transacciones **insertan**
> filas concurrentes en la misma partición.

Nuestro caso es exactamente "INSERTs concurrentes":

- HITL_A aprueba `expense_X`. Su `chunk_id_X` no existe en
  `expense_embeddings`. El MERGE cae en `WHEN NOT MATCHED → INSERT`.
- HITL_B aprueba `expense_Y` al mismo tiempo. Idem: cae en INSERT.
- Ambos van a `tenant_id = t_alpha`. Misma partición.
- Row-level no aplica (no hay row a "lockear" porque ninguno existía).
- Conflict en el commit: ambos quieren agregar archivos a la misma
  partición y el resolver no puede serializarlos.

Encima, la tabla es chica (13 archivos, 56 KB total). El poco data
skipping disponible hace que casi cualquier escritura toque casi
cualquier archivo. Aún con tablas grandes el problema persistiría: los
INSERTs en la misma partición siempre se ven como "appends conflictivos"
a menos que cambies el patrón.

**Conclusión de la investigación**: row-level concurrency no es la
herramienta correcta para nuestro caso. Necesitamos un patrón donde
los writers ni siquiera intenten coordinar.

---

## 7. La solución: Append-Only + Dedupe en Lectura

### 7.1 El insight

Delta **siempre** permite `INSERT INTO` concurrentes sin conflicto. Cada
INSERT crea archivos parquet nuevos (no toca los existentes), así que el
resolver no tiene nada que resolver.

El "costo" es que pueden quedar varias filas para el mismo `(chunk_id,
tenant_id)` cuando un expense se re-vectoriza. Resolvemos eso al **leer**
con `ROW_NUMBER()`, tomando la fila más reciente por `updated_at`.

### 7.2 Writer (`sync_vector.py`)

Antes (MERGE):

```sql
MERGE INTO gold.expense_embeddings t
USING (SELECT %s AS chunk_id, ...) s
ON t.chunk_id = s.chunk_id AND t.tenant_id = s.tenant_id
WHEN MATCHED THEN UPDATE SET embedding = s.embedding, updated_at = s.updated_at
WHEN NOT MATCHED THEN INSERT ...
```

Después (INSERT puro):

```sql
INSERT INTO gold.expense_embeddings (chunk_id, tenant_id, embedding, updated_at)
VALUES (%s, %s, ARRAY(...), current_timestamp())
```

Sin lookups, sin re-escrituras, sin coordinación. No conflicta.

### 7.3 Reader (`vector_search.py`)

Antes (LEFT JOIN directo, asume 1 fila por chunk):

```sql
SELECT c.*, e.embedding
FROM gold.expense_chunks c
LEFT JOIN gold.expense_embeddings e
  ON c.chunk_id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE c.tenant_id = %s
```

Después (CTE con dedupe por updated_at):

```sql
WITH latest_emb AS (
    SELECT chunk_id, tenant_id, embedding, updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY chunk_id, tenant_id
               ORDER BY updated_at DESC
           ) AS rn
    FROM gold.expense_embeddings
    WHERE tenant_id = %s
)
SELECT c.*, e.embedding
FROM gold.expense_chunks c
LEFT JOIN latest_emb e
  ON c.chunk_id = e.chunk_id AND c.tenant_id = e.tenant_id AND e.rn = 1
WHERE c.tenant_id = %s
```

Misma forma de respuesta para el caller. Cero cambios para el chat.

### 7.4 Trade-offs

| Aspecto | MERGE (antes) | INSERT-only (ahora) |
|---|---|---|
| Concurrencia | Conflicta bajo carga paralela | Nunca conflicta entre writers |
| Tamaño en S3 | Sin duplicados (UPDATE pisa) | Crece con re-vectorizaciones |
| Performance read | LEFT JOIN simple | LEFT JOIN + ROW_NUMBER |
| Mantenimiento | Ninguno | OPTIMIZE periódico para compactar duplicados |

El read overhead es despreciable: la CTE corre en el SQL warehouse, sólo
sobre filas del tenant, y `(chunk_id, tenant_id)` ya está particionado.
Estamos hablando de milisegundos.

El crecimiento en S3 es proporcional a re-vectorizaciones, que son raras
(sólo HITL aprueba o re-aprueba). En estado estacionario, ~1 fila por
chunk. Un job semanal `OPTIMIZE` + dedupe lo deja como nuevo.

---

## 8. Defensa en profundidad: el retry sigue ahí

INSERTs entre sí no chocan, pero pueden chocar con **operaciones de
mantenimiento**:

- `OPTIMIZE` corriendo en paralelo (compactación reescribe parquets, sí
  quita y agrega archivos).
- `VACUUM` borrando archivos viejos.
- Cambios de schema concurrentes (raro).

Por eso dejamos un retry acotado (4 intentos, backoff 1s/2s/4s) sobre
`DELTA_CONCURRENT_APPEND` en el activity. Defensa en profundidad sin
costo perceptible.

---

## 9. Lo que NO arregla esto

- **Si un mismo expense lanza dos `trigger_vector_sync` en paralelo**:
  ahora ambos hacen INSERT, no chocan, pero quedan **dos filas
  duplicadas** para el mismo `chunk_id`. El reader toma la más reciente
  por `updated_at`, así que el chat ve un valor consistente — pero la
  fila "perdedora" queda como basura. No es nuestro caso hoy (cada HITL
  aprueba un expense distinto), pero conviene cubrirlo con
  idempotency a futuro (ej: `INSERT ... WHERE NOT EXISTS` con check de
  embedding hash).

- **Crecimiento sin OPTIMIZE**: si nadie corre `OPTIMIZE` periódicamente,
  la tabla acumula filas duplicadas indefinidamente. Hoy no es un
  problema (volumen bajo), pero hay que agendarlo en `nexus-medallion`
  como job semanal.

---

## 10. Cómo lo vas a explicar (cheatsheet de 30 segundos)

> "El parquet es inmutable: para cambiar una fila hay que reescribir el
> archivo entero. Delta serializa esos cambios con un log central (estilo
> Git) y resuelve conflictos a nivel archivo. Probamos primero
> Deletion Vectors + Row Tracking, pero esa feature sólo cubre UPDATE/
> DELETE; nuestro caso son INSERTs concurrentes en la misma partición y
> esos siempre conflictan. La solución correcta es escribir
> append-only — los INSERTs nunca chocan entre sí porque cada uno crea
> archivos nuevos sin tocar los existentes — y deduplicar al leer
> tomando la fila más reciente por `updated_at`. Cero cambios de
> contrato para el lector, cero conflictos para los HITLs paralelos."

---

## 11. Aplicado en este repo

| Archivo | Cambio |
|---|---|
| `nexus-orchestration/src/nexus_orchestration/activities/sync_vector.py` | `MERGE` → `INSERT INTO ... VALUES`. Retry queda contra OPTIMIZE/VACUUM. `CREATE TABLE` mantiene `TBLPROPERTIES (DV, RowTracking)` como documentación (Databricks ya las pone por default). |
| `nexus-orchestration/src/nexus_orchestration/activities/vector_search.py` | LEFT JOIN ahora pasa por `WITH latest_emb AS (... ROW_NUMBER() OVER ... rn=1)`. Misma forma de respuesta. |

Verificación una vez deployado:
1. Lanzar 3+ HITLs concurrentes del mismo tenant.
2. Confirmar en Temporal que ningún `trigger_vector_sync` cae al branch
   `vector_sync_failed`.
3. Smoke-test el chat (debe encontrar embeddings recientes).
4. `SELECT chunk_id, COUNT(*) FROM gold.expense_embeddings GROUP BY 1
   HAVING COUNT(*) > 1` → debe ser vacío hasta que haya re-aprobaciones,
   y aún con duplicados el reader devuelve consistentes.

Pendiente (no urgente):
- Job semanal `OPTIMIZE nexus_dev.gold.expense_embeddings` + dedupe
  para compactar duplicados que se acumulen.
- Si en prod un mismo expense disparara `trigger_vector_sync` paralelo
  (no debería pasar; Temporal serializa), agregar idempotency-key.

---

## 12. Referencias

- Databricks docs · Isolation level + concurrency control:
  `https://docs.databricks.com/aws/en/optimizations/isolation-level`
- Databricks docs · Deletion Vectors:
  `https://docs.databricks.com/aws/en/delta/deletion-vectors`
- Delta Lake protocol · Row Tracking:
  `https://github.com/delta-io/delta/blob/master/PROTOCOL.md#row-tracking`
- Código relevante:
  - `nexus-orchestration/src/nexus_orchestration/activities/sync_vector.py`
    (writer — append-only)
  - `nexus-orchestration/src/nexus_orchestration/activities/vector_search.py`
    (reader — dedupe por ROW_NUMBER)
  - `nexus-medallion/src/gold/expense_chunks.py` (DLT MV vecino, sin cambios)
