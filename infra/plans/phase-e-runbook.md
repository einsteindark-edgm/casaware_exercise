# Phase E — Breadcrumb Dashboard Runbook

Quick deploy + validation steps for the X-Ray + ADOT + Databricks
breadcrumb stack landed in Phase E.

## 1. Deploy infra

```bash
cd infra/terraform
terraform plan -out plan.out      # expect: cluster setting, IAM, log group adot, dashboards, xray rules, sidecars
terraform apply plan.out
```

Expected new/changed resources:
- `aws_ecs_cluster.main` — `containerInsights = enhanced`
- `aws_iam_role_policy.{backend_task,worker_task}` — `xray:*` + `cloudwatch:PutMetricData`
- `aws_cloudwatch_log_group.adot`
- `aws_xray_sampling_rule.{default,critical_endpoints,chat_endpoints}`
- `aws_cloudwatch_dashboard.breadcrumb`
- ECS task definitions for backend + worker — adds `adot-collector` sidecar

## 2. Build + push images

`.github/workflows/build-and-push.yml` builds backend + worker and registers a new task-def revision per push to `main`. The new revision will pick up:
- the OTEL_* env vars (set in TF)
- the `adot-collector` sidecar (set in TF)
- the new `opentelemetry-*` python deps (built into the wheel)

After push, force a new deployment so old tasks (without sidecar) get replaced:

```bash
aws ecs update-service --cluster nexus --service nexus-backend --force-new-deployment
aws ecs update-service --cluster nexus --service nexus-worker  --force-new-deployment
```

## 3. Validate end-to-end

### a. ADOT collector is healthy

```bash
aws logs tail /ecs/nexus/adot --since 5m --follow
```

Expect lines like `Everything is ready. Begin running and processing data.` No `connection refused` to `localhost:4317`.

### b. Trace shows up in X-Ray

Make a real request:

```bash
curl -X POST https://<api>/v1/expenses \
  -H "Authorization: Bearer $TOKEN" \
  -F "expense_json={...}" -F "file=@receipt.jpg" -i
```

Capture the `X-Request-ID` from the response. Wait ~60s for the X-Ray batch flush.

Open ServiceLens — `https://<region>.console.aws.amazon.com/cloudwatch/home#servicelens:map` — and look for the `nexus-backend → nexus-worker` edge with non-zero RPS.

### c. trace_id reaches Mongo + Databricks

```bash
# In Mongo (replace expense_id):
db.expense_events.find({expense_id: "exp_..."}, {event_type:1, "metadata.trace_id":1, _id:0})
```

Every event should carry `metadata.trace_id` (32 hex chars).

In Databricks SQL:

```sql
SELECT trace_id, COUNT(*) FROM nexus_dev.silver.expense_events
WHERE created_at > current_timestamp() - INTERVAL 1 HOUR
  AND trace_id IS NOT NULL
GROUP BY trace_id ORDER BY 2 DESC LIMIT 10;
```

### d. Breadcrumb dashboard

Import the dashboard:

```bash
databricks dashboards create \
  --display-name "Nexus Breadcrumb" \
  --json @nexus-medallion/dashboards/nexus-breadcrumb.lvdash.json
```

Open it, pick a `trace_id` from the dropdown, and confirm the timeline + `stage_duration_sec` show up.

## 4. Cross-correlation cheatsheet

| Need to see... | Where |
|---|---|
| Live HTTP trace, p95 latency | CloudWatch ServiceLens |
| Service map (auto) | ServiceLens "Map" tab |
| Backend logs filtered by trace_id | CloudWatch dashboard `nexus-breadcrumb-live` |
| Long-running expense lifecycle (HITL takes hours) | Databricks dashboard `Nexus Breadcrumb` |
| Same expense, both views | Pick `trace_id` in either, paste in the other |

## 5. Rollback (if X-Ray cost surprises)

Quick downgrades, in order of impact:

```bash
# 1. Drop sampling to 1%
aws xray update-sampling-rule --sampling-rule-update '{"RuleName":"nexus-default","FixedRate":0.01}'

# 2. Disable Container Insights enhanced (back to basic)
# Edit ecs_cluster.tf: value = "enabled" (basic) → terraform apply

# 3. Disable OTel entirely (no spans emitted)
# Set OTEL_ENABLED=false in ECS task env, force-new-deployment
```

Logs + Mongo trace_id stays — only the X-Ray side stops collecting.

## 6. Known issues

- **First trace_ids in expense_events** show only on events emitted *after* deploy. Pre-deploy events stay NULL — gold join handles it (`first(trace_id, ignorenulls=True)`).
- **Temporal `TracingInterceptor` extra dep**: needs `temporalio[opentelemetry]>=1.7`. Currently we depend on `temporalio>=1.8`, which includes it. If install fails, pin the extra explicitly.
- **CDC tramo**: trace_id viaja en `metadata.trace_id` dentro del documento Mongo, así que el bronze→silver lo recibe sin tocar Debezium/Kafka headers. La propagación en Kafka headers queda fuera de E (ver Phase E.6 outline en el plan).
