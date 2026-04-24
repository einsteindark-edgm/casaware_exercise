# Fix · Propagación de HITL decisions + OCR → Medallion

> **Status**: Diagnosticado, listo para ejecutar.
> **Síntoma reportado**: `silver.hitl_events` no muestra `decision`, `resolved_fields`, `resolved_by`; `gold.expense_audit.final_amount` queda null cuando no hay HITL explícito; los conflictos de HITL no son consultables desde la capa gold.

---

## Diagnóstico

Hay **cinco bugs** en la cadena. Tres los describió el usuario; el cuarto apareció revisando el código HITL; el quinto apareció revisando la relación `receipts` ↔ `expenses`.

### Bug 1 — Backend no persiste la decisión del usuario en `hitl_tasks`

**Archivo**: `backend/src/nexus_backend/api/v1/hitl.py:87-90`

```python
await mongo.db.hitl_tasks.update_one(
    {"task_id": task_id},
    {"$set": {"status": "resolved", "resolved_at": now}},
)
```

El endpoint `POST /hitl/{task_id}/resolve` recibe `body.decision` y `body.resolved_fields` (línea 61), los manda como signal al workflow Temporal (líneas 76-85) y los vuelca en `expense_events` (líneas 92-110), **pero nunca los persiste en `hitl_tasks`**. Como el CDC lee desde `hitl_tasks`, la decisión nunca llega a bronze/silver/gold.

**Consecuencia medallion**:
- `bronze.mongodb_cdc_hitl_tasks` recibe un update event con solo `status=resolved`+`resolved_at`.
- `silver.hitl_events.decision`, `.resolved_fields`, `.resolved_by` → siempre NULL.
- `gold` no tiene desde dónde leer.

---

### Bug 2 — Mismatch de nombre de campo: `fields_in_conflict` vs `discrepancy_fields`

**Archivo**: `nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py:128`

```python
"fields_in_conflict": inp["fields_in_conflict"],
```

Pero el schema bronze (`nexus-medallion/src/bronze/cdc_hitl_tasks.py:25`) y silver (`silver/hitl_events.py:31`) esperan el campo con el nombre `discrepancy_fields`. El `from_json()` en bronze descarta silenciosamente campos no declarados → `discrepancy_fields` **siempre es NULL** en bronze, independientemente de lo que haya en Mongo.

**Este es probablemente el bug "los conflictos nunca llegan"** — no es que silver no los use; es que el nombre está roto en origen.

**Consecuencia medallion**: `bronze.mongodb_cdc_hitl_tasks.discrepancy_fields` siempre NULL → `silver.hitl_events.discrepancy_fields` siempre NULL → los conflictos son invisibles en toda la capa analítica.

---

### Bug 3 — OCR total solo llega a `expenses.final_amount` si el workflow completa aprobación

**Archivo**: `nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py:33-52`

```python
@activity.defn(name="upsert_ocr_extraction")
async def upsert_ocr_extraction(inp: dict[str, Any]) -> None:
    doc = {
        ...
        "ocr_total": (inp.get("ocr_total") or {}).get("value"),
        ...
    }
    await _db().ocr_extractions.update_one(
        {"expense_id": inp["expense_id"]}, {"$set": doc}, upsert=True
    )
```

`upsert_ocr_extraction` escribe solamente a `ocr_extractions`, nunca a `expenses`. El valor OCR llega a `expenses.final_amount` **solo** cuando el workflow invoca `update_expense_to_approved` (mongodb_writes.py:64-83), que requiere que el expense llegue a aprobación final (happy path sin HITL O HITL resuelto).

**Consecuencia medallion** (para cualquier expense en estado pending/HITL/cancelado/timeout):
- `silver.expenses.final_amount` = NULL.
- `silver.expenses.amount` = el valor user-reported original (sin corrección OCR).
- `gold.expense_audit` filtra por `status='approved'` (`gold/expense_audit.py:34`) → esos expenses ni siquiera aparecen, pero para los que sí aprueban sin HITL explícito, el `coalesce(final_amount, amount)` puede caer en `amount` si `final_data.amount` no trae el OCR.
- `silver.ocr_extractions.ocr_total` tiene el dato correcto pero **nadie hace el join** con `silver.expenses` para exponerlo en gold.

---

### Bug 4 — No hay visibilidad analítica de conflictos HITL

Incluso después de arreglar los Bugs 1-2, `silver.hitl_events.discrepancy_fields` queda como `Array<String>` (ej. `["amount", "vendor"]`) que BI tiene que parsear. No hay tabla gold que permita:
- Contar "¿qué campo entra más en conflicto?" (explode del array).
- Join con `gold.expense_audit` para saber "¿cuántos expenses aprobados pasaron por HITL y qué cambiaron?".

Este es el bug que el usuario dejó a mi criterio. Propongo resolverlo con:
1. Una tabla `silver.hitl_discrepancies` que explode `discrepancy_fields` (una fila por conflicto).
2. Columnas nuevas en `gold.expense_audit`: `had_hitl`, `hitl_decision`, `hitl_conflict_fields`, `hitl_resolved_by`.

---

### Bug 5 — `receipts` no persiste `expense_id` en Mongo (relación asimétrica)

**Archivo**: `backend/src/nexus_backend/api/v1/expenses.py:110-121`

```python
await mongo.db.receipts.insert_one(
    {
        "receipt_id": receipt_id,
        "tenant_id": user.tenant_id,
        "user_id": user.sub,
        "s3_bucket": settings.s3_receipts_bucket,
        "s3_key": s3_key,
        ...
    }
)
```

El insert de `receipts` no incluye `expense_id`, pero inmediatamente después (línea 123-139) el insert de `expenses` sí incluye `receipt_id`. La relación queda unidireccional en Mongo.

El schema bronze `cdc_receipts.py:21` **ya declara** `expense_id` como StringType — se pensó que iba a estar ahí, pero nadie lo escribe → `bronze.mongodb_cdc_receipts.expense_id` siempre NULL.

**Por qué corregirlo** (no solo por simetría):
- **Relación 1:1 real**: el código escribe receipt + expense consecutivamente en el mismo endpoint. No hay receipts huérfanos ni compartidos.
- **Join bidireccional**: queries tipo "¿qué expenses se aprobaron de los recibos subidos el martes?" hoy requieren doble hop via `expenses`.
- **Contrato bronze roto**: el campo existe en el schema pero nunca llega con valor.
- **CDC-first**: cualquier consumer futuro del topic `nexus.nexus_dev.receipts` necesita saber a qué expense pertenece sin re-consultar Mongo.

**Consecuencia medallion**: `silver.receipts` (si existe) y cualquier join por `expense_id` desde receipts queda vacío.

---

## Fix · Cambios concretos

### F1 — Backend: persistir decision + resolved_fields + resolved_by en `hitl_tasks`

**Archivo**: `backend/src/nexus_backend/api/v1/hitl.py:87-90`

**Antes:**
```python
await mongo.db.hitl_tasks.update_one(
    {"task_id": task_id},
    {"$set": {"status": "resolved", "resolved_at": now}},
)
```

**Después:**
```python
await mongo.db.hitl_tasks.update_one(
    {"task_id": task_id},
    {"$set": {
        "status": "resolved",
        "resolved_at": now,
        "decision": body.decision,
        "resolved_fields": body.resolved_fields,
        "resolved_by": user.sub,
    }},
)
```

**Por qué el backend y no el worker**: la docstring de `mongodb_writes.py:1-7` ya asigna explícitamente a backend el ownership de "hitl_tasks.status=resolved transition". Ampliar ese mismo update con los campos de decisión respeta la separación existente y no duplica escrituras.

### F2 — Worker: renombrar `fields_in_conflict` → `discrepancy_fields`

**Archivo**: `nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py:128`

**Antes:**
```python
"fields_in_conflict": inp["fields_in_conflict"],
```

**Después:**
```python
"discrepancy_fields": inp["fields_in_conflict"],
```

Renombrar solo el key en el documento Mongo. El caller del workflow sigue pasando `inp["fields_in_conflict"]` (no tocar el contrato interno del workflow).

**Alternativa descartada**: cambiar bronze/silver para leer `fields_in_conflict`. Requiere tocar 2 notebooks más + romper retrocompatibilidad con cualquier otro consumer que ya lea `discrepancy_fields`. Renombrar en el worker es un cambio de 1 línea y alinea con la convención del schema downstream.

### F3 — Worker: escribir OCR total a `expenses` al extraer (independiente de aprobación)

Hay dos caminos posibles para que `silver.expenses` tenga acceso al OCR total:

**Opción A — Escribir a `expenses.ocr_candidate_*` cuando OCR extrae**:

**Archivo**: `nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py:33-52`

Agregar un write adicional al final de `upsert_ocr_extraction`:

```python
@activity.defn(name="upsert_ocr_extraction")
async def upsert_ocr_extraction(inp: dict[str, Any]) -> None:
    doc = { ... }  # sin cambios
    await _db().ocr_extractions.update_one(
        {"expense_id": inp["expense_id"]}, {"$set": doc}, upsert=True
    )
    # Propagar candidates a expenses así el CDC los lleva a silver.expenses,
    # independiente de que el workflow complete aprobación o no.
    await _db().expenses.update_one(
        {"expense_id": inp["expense_id"], "tenant_id": inp["tenant_id"]},
        {"$set": {
            "ocr_amount_candidate": doc["ocr_total"],
            "ocr_vendor_candidate": doc["ocr_vendor"],
            "ocr_date_candidate": doc["ocr_date"],
            "updated_at": _now(),
        }},
    )
```

Agregar los 3 campos al schema bronze (`cdc_expenses.py`) y silver (`expenses.py`).

**Opción B — No escribir a `expenses`; hacer el join en silver/gold** (recomendada):

**Archivo**: `nexus-medallion/src/gold/expense_audit.py`

Cambiar el fallback para considerar también `silver.ocr_extractions`:

```python
@dlt.table(name="expense_audit", ...)
def expense_audit():
    expenses = dlt.read("silver.expenses").where("status = 'approved'")
    ocr = dlt.read(f"{spark.conf.get('nexus.catalog')}.silver.ocr_extractions")
    return (
        expenses.join(ocr, on="expense_id", how="left")
        .select(
            "tenant_id",
            "expense_id",
            # ... resto igual, pero:
            coalesce(col("final_amount"), col("ocr_total"), col("amount")).alias("final_amount"),
            coalesce(col("final_vendor"), col("ocr_vendor"), col("vendor")).alias("final_vendor"),
            coalesce(col("final_date"), col("ocr_date"), col("date")).alias("final_date"),
            coalesce(col("final_currency"), col("ocr_currency"), col("currency")).alias("final_currency"),
            # ...
        )
    )
```

**Recomiendo Opción B**:
- Cambio confinado a medallion — no rescribe Mongo ni afecta al worker.
- No cambia el contrato de `expenses` (ya cargada de meaning con `final_*` + `amount`).
- El join es barato: `silver.ocr_extractions` tiene ≤ 1 row por expense.
- La semántica queda clara: "si hay final_amount humano, gana; si no, OCR; si no, lo reportado por el user".

### F4 — Silver nueva tabla `hitl_discrepancies` (explode de conflictos)

**Archivo nuevo**: `nexus-medallion/src/silver/hitl_discrepancies.py`

```python
# Databricks notebook source
# MAGIC %md
# MAGIC # silver.hitl_discrepancies
# MAGIC
# MAGIC Una fila por (task_id, field_in_conflict). Explode del array
# MAGIC `discrepancy_fields` de hitl_events para facilitar BI queries:
# MAGIC "¿qué campo entra más seguido en conflicto?".

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, explode


@dlt.table(
    name="hitl_discrepancies",
    comment="Un conflicto por fila — derivado de silver.hitl_events.discrepancy_fields.",
    table_properties={"quality": "silver"},
)
def hitl_discrepancies():
    return (
        dlt.read("hitl_events")
        .where("discrepancy_fields IS NOT NULL AND size(discrepancy_fields) > 0")
        .select(
            col("task_id"),
            col("tenant_id"),
            col("expense_id"),
            col("workflow_id"),
            explode(col("discrepancy_fields")).alias("field_name"),
            col("decision"),
            col("resolved_by"),
            col("resolved_at"),
            col("created_at"),
        )
    )
```

Registrarlo en `nexus-medallion/resources/pipelines/silver.yml` (libraries).

### F5 — Gold: enriquecer `expense_audit` con metadatos HITL

**Archivo**: `nexus-medallion/src/gold/expense_audit.py`

Agregar left join con `silver.hitl_events` (por `expense_id` — puede haber N:1 si hay más de un HITL por expense; tomar el más reciente).

```python
from pyspark.sql.functions import coalesce, col, max as spark_max
from pyspark.sql.window import Window

@dlt.table(name="expense_audit", ...)
def expense_audit():
    expenses = dlt.read("silver.expenses").where("status = 'approved'")
    ocr = dlt.read(f"{spark.conf.get('nexus.catalog')}.silver.ocr_extractions")
    hitl = (
        dlt.read("silver.hitl_events")
        .where("status = 'resolved'")
        # Última resolución por expense:
        .groupBy("expense_id").agg(
            spark_max("resolved_at").alias("hitl_resolved_at"),
            spark_max("decision").alias("hitl_decision"),
            spark_max("resolved_by").alias("hitl_resolved_by"),
            spark_max("discrepancy_fields").alias("hitl_conflict_fields"),
        )
    )
    return (
        expenses
        .join(ocr, on="expense_id", how="left")
        .join(hitl, on="expense_id", how="left")
        .select(
            "tenant_id", "expense_id", "user_id", "receipt_id",
            coalesce(col("final_amount"), col("ocr_total"), col("amount")).alias("final_amount"),
            coalesce(col("final_vendor"), col("ocr_vendor"), col("vendor")).alias("final_vendor"),
            coalesce(col("final_date"), col("ocr_date"), col("date")).alias("final_date"),
            coalesce(col("final_currency"), col("ocr_currency"), col("currency")).alias("final_currency"),
            "category", "source_per_field",
            coalesce(col("approved_at"), col("updated_at")).alias("approved_at"),
            # HITL metadata
            col("hitl_decision").isNotNull().alias("had_hitl"),
            col("hitl_decision"),
            col("hitl_conflict_fields"),
            col("hitl_resolved_by"),
            col("hitl_resolved_at"),
        )
    )
```

---

### F6 — Backend: persistir `expense_id` en `receipts`

**Archivo**: `backend/src/nexus_backend/api/v1/expenses.py:110-121`

**Antes:**
```python
await mongo.db.receipts.insert_one(
    {
        "receipt_id": receipt_id,
        "tenant_id": user.tenant_id,
        "user_id": user.sub,
        "s3_bucket": settings.s3_receipts_bucket,
        ...
    }
)
```

**Después:**
```python
await mongo.db.receipts.insert_one(
    {
        "receipt_id": receipt_id,
        "tenant_id": user.tenant_id,
        "user_id": user.sub,
        "expense_id": expense_id,     # ← NUEVO
        "s3_bucket": settings.s3_receipts_bucket,
        ...
    }
)
```

El `expense_id` ya está disponible en ese scope (generado en la misma función antes del insert). No se toca el schema bronze porque ya declara el campo.

---

## Archivos modificados/creados

| Archivo | Tipo | Bug(s) |
|---|---|---|
| `backend/src/nexus_backend/api/v1/hitl.py` | Modify (líneas 87-90) | F1 |
| `backend/src/nexus_backend/api/v1/expenses.py` | Modify (línea 110-121) | F6 |
| `nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py` | Modify (línea 128) | F2 |
| `nexus-medallion/src/gold/expense_audit.py` | Rewrite | F3 (Opción B), F5 |
| `nexus-medallion/src/silver/hitl_discrepancies.py` | Create | F4 |
| `nexus-medallion/resources/pipelines/silver.yml` | Modify (agregar library) | F4 |

No hay que tocar:
- Los notebooks bronze (el schema ya tiene `discrepancy_fields` con el nombre correcto — es Mongo el que escribe mal).
- `silver/expenses.py`, `silver/hitl_events.py`, `silver/ocr_extractions.py` — el join nuevo vive en gold, no en silver.
- `nexus-cdc/` — ya eliminado en Phase B+.
- Debezium config — el nombre del campo en Mongo cambia pero Debezium propaga el documento entero como JSON, se autoadapta.

---

## Datos históricos

Los expenses ya procesados con:

- `hitl_tasks.fields_in_conflict` (nombre viejo): el array queda huérfano en Mongo. Dos opciones:
  - **Backfill Mongo** (recomendado): script one-shot que hace `$rename: {fields_in_conflict: discrepancy_fields}` sobre toda la colección. Corre idempotente.
  - **Aceptar pérdida histórica**: los expenses pre-fix quedan sin conflictos visibles. En dev es aceptable si el volumen es chico.

- `hitl_tasks.status=resolved` sin decision/resolved_fields: no hay fuente de verdad para reconstruir. `expense_events` tipo `hitl_resolved` sí tiene ambos campos (hitl.py:102-104) → podría hacerse un backfill cruzando `expense_events`→`hitl_tasks`. Documentarlo como follow-up si el usuario lo pide.

- `receipts` sin `expense_id`: recuperable al 100% porque cada `expenses` doc tiene su `receipt_id`. Backfill incluido abajo.

### Script de backfill sugerido (mongosh)

```javascript
use nexus_dev

// Rename del campo del array
const r1 = db.hitl_tasks.updateMany(
  { fields_in_conflict: { $exists: true } },
  { $rename: { "fields_in_conflict": "discrepancy_fields" } }
);
print("Renamed fields_in_conflict → discrepancy_fields:", r1.modifiedCount);

// Backfill decision/resolved_fields/resolved_by desde expense_events
const cursor = db.expense_events.find({ event_type: "hitl_resolved" });
let patched = 0;
while (cursor.hasNext()) {
  const ev = cursor.next();
  const taskId = ev.details?.hitl_task_id;
  if (!taskId) continue;
  const r = db.hitl_tasks.updateOne(
    { task_id: taskId, decision: { $exists: false } },
    { $set: {
        decision: ev.details?.decision,
        resolved_fields: ev.details?.resolved_fields,
        resolved_by: ev.actor?.id,
    }}
  );
  patched += r.modifiedCount;
}
print("Backfilled hitl decisions:", patched);

// Backfill receipts.expense_id desde expenses.receipt_id
const expCursor = db.expenses.find(
  { receipt_id: { $exists: true } },
  { expense_id: 1, receipt_id: 1, _id: 0 }
);
let receipts_patched = 0;
while (expCursor.hasNext()) {
  const e = expCursor.next();
  const r = db.receipts.updateOne(
    { receipt_id: e.receipt_id, expense_id: { $exists: false } },
    { $set: { expense_id: e.expense_id } }
  );
  receipts_patched += r.modifiedCount;
}
print("Backfilled receipts.expense_id:", receipts_patched);
```

Correr **antes** de aplicar F1+F2+F6 evita que haya una ventana donde los writes nuevos escriben bien pero los viejos siguen sin arreglar.

---

## Verificación end-to-end

Después de aplicar los 5 fixes + backfill Mongo + refresh del pipeline silver/gold:

```sql
-- 1. Los conflictos ya llegan a bronze/silver
SELECT COUNT(*) AS non_null_conflicts
FROM nexus_dev.silver.hitl_events
WHERE discrepancy_fields IS NOT NULL AND size(discrepancy_fields) > 0;
-- Esperado: > 0 post-backfill.

-- 2. La decisión del usuario se ve en silver
SELECT task_id, status, decision, resolved_by, resolved_fields
FROM nexus_dev.silver.hitl_events
WHERE status = 'resolved';
-- Esperado: decision, resolved_by, resolved_fields populados.

-- 3. Gold combina las tres fuentes correctamente
SELECT
  expense_id,
  amount,
  final_amount,  -- debe tomar: corrección humana > OCR > reportado user
  had_hitl,
  hitl_decision,
  hitl_conflict_fields
FROM nexus_dev.gold.expense_audit
ORDER BY approved_at DESC
LIMIT 20;
-- Esperado: expenses aprobados con HITL muestran hitl_decision + conflict_fields;
-- expenses aprobados sin HITL muestran final_amount = OCR total si no hubo corrección.

-- 4. La nueva tabla de discrepancias es consultable
SELECT field_name, COUNT(*) AS n
FROM nexus_dev.silver.hitl_discrepancies
GROUP BY field_name ORDER BY n DESC;
-- Esperado: ranking de qué campo entra más en conflicto.
```

---

## Orden de ejecución

1. **Mongo backfill** (script arriba). Idempotente — se puede correr antes del deploy.
2. **Merge F1+F6** (backend) → rebuild + redeploy backend. Desde este momento: nuevas resoluciones HITL persisten decision/resolved_fields/resolved_by, y nuevos recibos persisten expense_id.
3. **Merge F2** (worker `mongodb_writes.py`) → rebuild + redeploy worker. Desde este momento, todos los HITL tasks nuevos escriben `discrepancy_fields`.
4. **Merge F4** (silver hitl_discrepancies notebook + silver.yml).
5. **Merge F3+F5** (gold rewrite).
6. En Databricks: `databricks bundle deploy --target dev` + `databricks pipelines start silver_pipeline --full-refresh` + `databricks pipelines start gold_pipeline --full-refresh`.
7. Correr las queries de verificación.

Los 6 cambios juntos llevan el pipeline de "decisiones invisibles + OCR perdido + conflictos ocultos + receipts sin relación" → "todo trazable hasta gold con la corrección humana ganando sobre OCR ganando sobre lo reportado por el user, y receipts enlazados a expenses en ambos sentidos".
