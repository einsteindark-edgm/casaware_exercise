# Tablero 1 — Nexus Ops (MongoDB Atlas Charts)

Dashboard operativo tiempo (casi) real sobre `nexus_dev`. Fuente directa =
cluster Atlas (no ETL). Refresh cada 60 s, 9 widgets + 2 filtros globales.

---

## 0. Setup — una vez por dashboard

1. Entra a Atlas → **Charts** → **Add Dashboard**.
2. Nombre: `Nexus Ops`.
3. **+ Add Data Source**: selecciona cluster → DB `nexus_dev` → añade las
   colecciones que usaremos:
   - `expenses`
   - `receipts`
   - `ocr_extractions`
   - `hitl_tasks`
   - `expense_events`
   - `chat_turns`
4. **Refresh**: ícono del dashboard → `Refresh settings` → `Every 1 minute`.
5. **Theme** (opcional): toggle dark.

### Filtros globales del dashboard

Dashboard → **Add Filter** (arriba a la derecha):

| Nombre | Tipo | Field | Data Source | Default |
|---|---|---|---|---|
| Tenant | String (Autocomplete) | `tenant_id` | `expenses` | *(vacío = todos)* |
| Rango | Date Range | `created_at` | `expenses` | Last 24 hours |

Marca "Apply to all charts that include these fields". Los widgets que abajo
usen `tenant_id` / `created_at` los heredarán automáticamente.

---

## 1. Widgets — create → **+ Add Chart** y pega cada pipeline

Para cada widget: elige **Data Source = {colección}**, pestaña
**Aggregation pipeline** (no Chart Builder), pega el pipeline, luego en
**Chart type** eliges lo indicado.

> Tip: si un stage te sale en rojo por sintaxis, verifica que pegaste el
> objeto completo incluyendo el `[` y `]`.

### 1.1 Expenses por status — Donut
Data source: `expenses`
```json
[
  { "$group": { "_id": "$status", "n": { "$sum": 1 } } }
]
```
Chart type: **Donut** → Label = `_id`, Value = `n`.

### 1.2 Volumen de expenses por hora — Line
Data source: `expenses`
```json
[
  {
    "$group": {
      "_id": { "$dateTrunc": { "date": "$created_at", "unit": "hour" } },
      "n": { "$sum": 1 }
    }
  },
  { "$sort": { "_id": 1 } }
]
```
Chart type: **Continuous Line** → X = `_id` (Binning: Hour), Y = `n`.

### 1.3 Monto aprobado vs rechazado del día — Grouped Bar
Data source: `expenses`
```json
[
  { "$match": { "status": { "$in": ["approved", "rejected"] } } },
  {
    "$group": {
      "_id": {
        "day": { "$dateTrunc": { "date": "$updated_at", "unit": "day" } },
        "status": "$status"
      },
      "total": { "$sum": "$amount" }
    }
  },
  { "$sort": { "_id.day": 1 } }
]
```
Chart type: **Grouped Column** → X = `_id.day`, Y = `total`, Series = `_id.status`.

### 1.4 HITL pendientes — Number (alerta)
Data source: `hitl_tasks`
```json
[
  { "$match": { "status": "pending" } },
  { "$count": "pending" }
]
```
Chart type: **Number** → Value = `pending`.
Conditional formatting → rule: `> 5` pinta rojo.

### 1.5 Tiempo de resolución HITL (p50/p95) — Heatmap por tenant
Data source: `hitl_tasks`
```json
[
  { "$match": { "status": "resolved" } },
  {
    "$project": {
      "tenant_id": 1,
      "duration_min": {
        "$divide": [
          { "$subtract": ["$resolved_at", "$created_at"] },
          60000
        ]
      },
      "day": { "$dateTrunc": { "date": "$created_at", "unit": "day" } }
    }
  },
  {
    "$group": {
      "_id": { "tenant_id": "$tenant_id", "day": "$day" },
      "p50": { "$percentile": { "input": "$duration_min", "p": [0.5], "method": "approximate" } },
      "p95": { "$percentile": { "input": "$duration_min", "p": [0.95], "method": "approximate" } },
      "n":   { "$sum": 1 }
    }
  }
]
```
Chart type: **Heatmap** → X = `_id.day`, Y = `_id.tenant_id`, Intensity = `p95` (array[0]).

### 1.6 OCR con baja confianza — Table
Data source: `ocr_extractions`
```json
[
  { "$match": { "avg_confidence": { "$lt": 80 } } },
  {
    "$project": {
      "_id": 0,
      "expense_id": 1,
      "tenant_id": 1,
      "avg_confidence": 1,
      "ocr_total": 1,
      "ocr_vendor": 1,
      "ocr_date": 1,
      "extracted_at": 1
    }
  },
  { "$sort": { "extracted_at": -1 } },
  { "$limit": 50 }
]
```
Chart type: **Table**.

### 1.7 Expenses atascadas en processing — Table (alerta roja)
Data source: `expenses`
```json
[
  {
    "$match": {
      "status": "processing",
      "$expr": {
        "$lt": [
          "$updated_at",
          { "$dateSubtract": { "startDate": "$$NOW", "unit": "minute", "amount": 10 } }
        ]
      }
    }
  },
  {
    "$project": {
      "_id": 0,
      "expense_id": 1,
      "tenant_id": 1,
      "workflow_id": 1,
      "updated_at": 1,
      "stuck_minutes": {
        "$divide": [
          { "$subtract": ["$$NOW", "$updated_at"] },
          60000
        ]
      }
    }
  },
  { "$sort": { "stuck_minutes": -1 } }
]
```
Chart type: **Table**. Conditional formatting en `stuck_minutes > 30` → fondo rojo.

### 1.8 Chat turns por día — Area
Data source: `chat_turns`
```json
[
  {
    "$group": {
      "_id": { "$dateTrunc": { "date": "$created_at", "unit": "day" } },
      "turns": { "$sum": 1 }
    }
  },
  { "$sort": { "_id": 1 } }
]
```
Chart type: **Stacked Area**.

### 1.9 Top vendors del mes — Horizontal Bar
Data source: `expenses`
```json
[
  {
    "$match": {
      "$expr": {
        "$gte": [
          "$created_at",
          { "$dateSubtract": { "startDate": "$$NOW", "unit": "day", "amount": 30 } }
        ]
      }
    }
  },
  {
    "$group": {
      "_id": "$vendor",
      "n": { "$sum": 1 },
      "total": { "$sum": "$amount" }
    }
  },
  { "$sort": { "total": -1 } },
  { "$limit": 10 }
]
```
Chart type: **Horizontal Bar** → Y = `_id`, X = `total`, Label = `n`.

---

## 2. Layout sugerido (drag & drop)

```
┌─────────────────────┬──────────────┬──────────────┐
│ 1.2 Volumen/hora    │ 1.4 HITL pend│ 1.1 Status   │
│ (line, 2 cols)      │ (big number) │ (donut)      │
├─────────────────────┴──────────────┴──────────────┤
│ 1.7 Processing stuck (table 3-col)                │
├─────────────────────┬──────────────┬──────────────┤
│ 1.5 HITL heatmap    │ 1.3 Aprob/rej│ 1.6 OCR <80% │
├─────────────────────┼──────────────┴──────────────┤
│ 1.8 Chat turns      │ 1.9 Top vendors del mes     │
└─────────────────────┴─────────────────────────────┘
```

## 3. Alertas por email (feature 2026)

Dashboard → `···` → **Schedule email delivery** → diariamente 9:00 local →
incluir los widgets 1.4, 1.7 y 1.1. Enviar a ti + al buzón ops.

## 4. Compartir (solo-lectura)

Dashboard → `···` → **Embed Dashboard** → genera un unauthenticated URL si
quieres proyectarlo en una TV; o usa authenticated embed si lo metes en el
frontend Next.js más adelante (el SDK soporta filtrado por claim del JWT).

---

## 5. Checklist de verificación

- [ ] Los 6 data sources agregados al dashboard.
- [ ] Filtro "Tenant" aplicado a los 9 widgets.
- [ ] Filtro "Rango" aplicado a los widgets con `created_at`.
- [ ] 1.4 muestra 0 si limpiaste HITL; si muestra >5 con fondo rojo, la
      alerta visual funciona.
- [ ] 1.7 vacío cuando no hay workflows en `processing`. Sube un recibo al
      backend; debe aparecer una fila y desaparecer cuando llegue a HITL.
- [ ] 1.2 y 1.8 solo se ven si hay datos en la ventana; si la DB está fría,
      ajusta el filtro de rango a "Last 30 days".

---

## 6. Siguiente paso

Cuando este esté pintado y verificado, pasamos al **Tablero 3 — Pipeline
observability** en Databricks AI/BI. Ese vive sobre `system.lakeflow.*` y
te da visibilidad del CDC + DLT con 4 consultas SQL.
