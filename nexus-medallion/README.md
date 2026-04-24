# nexus-medallion

Databricks Asset Bundle con las pipelines Lakeflow (silver, gold) + jobs one-shot (seed desde Mongo, Vector Search setup) del stack Nexus.

## Layout

```
nexus-medallion/
├── databricks.yml                # Bundle root
├── resources/
│   ├── pipelines/                # Lakeflow pipelines (triggered en dev)
│   │   ├── silver.yml
│   │   └── gold.yml
│   └── jobs/                     # Jobs one-shot
│       ├── seed_bronze.yml
│       └── vector_setup.yml
├── src/
│   ├── seed/                     # C.3: seed desde Mongo Atlas
│   ├── silver/                   # C.4: APPLY CHANGES INTO (SCD1)
│   ├── gold/                     # C.5: expense_audit + expense_chunks
│   ├── vector/                   # C.6: endpoint + Delta Sync index
│   └── common/                   # helpers compartidos (schemas, consts)
└── tests/
    ├── unit/
    └── integration/
```

## Setup

Prerequisitos: Databricks CLI v0.220+, workspace creado en C.0, Unity Catalog metastore asignado, PAT generado.

```bash
# Configurar profile (una vez)
databricks configure --profile nexus-dev --host https://dbc-...cloud.databricks.com
# pegar PAT cuando lo pida

# Validar
databricks bundle validate --target dev --profile nexus-dev

# Desplegar (sube notebooks y define pipelines/jobs en el workspace)
databricks bundle deploy --target dev --profile nexus-dev
```

## Ejecución

```bash
# Seed inicial (una vez, tras phase C.3)
databricks bundle run seed_bronze --target dev --profile nexus-dev

# Pipelines triggered (ejecutan y paran; no continuous para ahorrar costos)
databricks bundle run silver_pipeline --target dev --profile nexus-dev
databricks bundle run gold_pipeline   --target dev --profile nexus-dev

# Vector Search setup (una vez)
databricks bundle run vector_setup --target dev --profile nexus-dev
```

## Verificación rápida

En el SQL Editor del workspace:

```sql
-- Filas llegaron a silver
SELECT status, COUNT(*) FROM nexus_dev.silver.expenses GROUP BY status;

-- Gold solo tiene approved
SELECT tenant_id, COUNT(*) FROM nexus_dev.gold.expense_audit GROUP BY tenant_id;

-- Chunks listos para RAG
SELECT chunk_id, chunk_text FROM nexus_dev.gold.expense_chunks LIMIT 5;
```

## Cost control

Los pipelines Lakeflow están configurados como **triggered** (no continuous) para no correr 24/7. El endpoint Vector Search STANDARD escala a 0 cuando no hay queries. Cuando no estés usando el bundle, simplemente no lo ejecutes — el costo idle es solo el bucket S3 UC (~$0.03/mes).
