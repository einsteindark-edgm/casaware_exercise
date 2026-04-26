# Incidente 2026-04-26 — HITL approved no llega a `gold.expense_audit`

> Análisis post-mortem y lecciones aprendidas. El bug se manifestaba como
> "el usuario aprueba un HITL en el frontend, los valores se sobreescriben
> en Mongo, pero el expense nunca aparece en `gold.expense_audit`".
> En realidad eran **4 bugs encadenados** en 3 capas distintas del sistema.
> Este doc traza cada capa, qué la rompía, cómo se arregló, y qué se
> aprendió.

## Tabla de contenidos

1. [Síntoma observable](#1-síntoma-observable)
2. [Mapa de capas afectadas](#2-mapa-de-capas-afectadas)
3. [Capa 1: Workflow — rejection-on-vector-sync-timeout](#3-capa-1-workflow--rejection-on-vector-sync-timeout)
4. [Capa 2: Debezium streaming — ClassCastException String → byte[]](#4-capa-2-debezium-streaming--classcastexception-string--byte)
5. [Capa 3: MSK — auto.create.topics.enable=false](#5-capa-3-msk--autocreatetopicsenable--false)
6. [Capa 4 (cosmético): Quarkus env-var mapping](#6-capa-4-cosmético-quarkus-env-var-mapping)
7. [Diagnóstico — orden y herramientas](#7-diagnóstico--orden-y-herramientas)
8. [Lecciones aprendidas](#8-lecciones-aprendidas)
9. [Verificación end-to-end post-fix](#9-verificación-end-to-end-post-fix)

---

## 1. Síntoma observable

Tres observaciones del usuario que parecían inconsistentes:

| Capa | Estado |
|---|---|
| Mongo `expenses` | `status="rejected"`, `final_amount=145500 COP`, `rejection_reason="VECTOR_SYNC_TIMEOUT"` |
| `silver.expenses` | `status="hitl_required"` (estado anterior congelado) |
| `gold.expense_audit` | sin fila para el expense |
| `bronze.mongodb_cdc_expenses` | sólo eventos con `__op='r'` (snapshot), nunca `__op='u'/'d'` (streaming) |

La inconsistencia "Mongo dice rejected, silver dice hitl_required" inmediatamente sugiere que **el CDC no estaba propagando cambios**. Y la ausencia de `__op='u'` en bronze lo confirma: Debezium estaba snapshotteando pero no streameando.

## 2. Mapa de capas afectadas

```
┌────────────────────────────────────────────────────────────────────┐
│  Frontend → POST /api/v1/hitl/{id}/resolve → Backend              │
│  Backend → Temporal signal → ExpenseAuditWorkflow                 │
│                                                                    │
│  ┌──── CAPA 1: Workflow ──────────────────────────────┐           │
│  │  update_expense_to_approved (Mongo)  ✓             │           │
│  │  trigger_vector_sync (poll gold 18min) ✗ TIMEOUT   │           │
│  │  _fail → update_expense_to_rejected ← BUG #1       │           │
│  │     (rolling-back destruye el approved final)      │           │
│  └────────────────────────────────────────────────────┘           │
│                          │                                         │
│                          ▼                                         │
│  ┌──── CAPA 2: Debezium → Kafka ────────────────────────┐         │
│  │  Mongo change stream → Debezium 3.0.0.Final         │         │
│  │  Snapshots OK (__op='r')                             │         │
│  │  Streaming (__op='u') → ClassCastException          │         │
│  │     String cannot be cast to [B  ← BUG #2            │         │
│  │  Engine crashea, ECS reinicia, ciclo infinito       │         │
│  └──────────────────────────────────────────────────────┘         │
│                          │                                         │
│                          ▼                                         │
│  ┌──── CAPA 3: MSK Provisioned ──────────────────────────┐        │
│  │  auto.create.topics.enable=false  ← BUG #3           │        │
│  │  chat_turns / chat_sessions topics no existen        │        │
│  │  Producer timeout 60s al publicar a esos topics      │        │
│  │  Connector engine crashea (efecto colateral)         │        │
│  └──────────────────────────────────────────────────────┘        │
│                          │                                         │
│                          ▼                                         │
│   bronze (DLT streaming) → silver (apply_changes SCD1) →           │
│   gold.expense_audit (where status='approved')                     │
└────────────────────────────────────────────────────────────────────┘
```

**Observación clave**: aunque BUG #1 era el único directamente visible al usuario (rejected en Mongo), arreglarlo solo no resolvía nada porque BUG #2 mataba la propagación de CDC, así que aún sin la rejection, el medallion no veía el approved. Y BUG #3 mantenía a Debezium en crash loop incluso después de #2. Los tres tenían que arreglarse para que el flujo end-to-end funcionara.

---

## 3. Capa 1: Workflow — rejection-on-vector-sync-timeout

### 3.1 Qué pasaba

Después del HITL signal, `ExpenseAuditWorkflow` (`nexus-orchestration/src/nexus_orchestration/workflows/expense_audit.py`) hacía:

```python
# 3) update_expense_to_approved   → Mongo: status="approved", final_amount, ...
# 4) emit_expense_event "approved"
# 5) trigger_vector_sync           → polling gold.expense_chunks 18min
#    if vec_status in ("failed", "timed_out_not_in_gold"):
#        await self._fail(...)     ← ESTO LLAMA update_expense_to_rejected
#        return {"status": "failed"}
# 6) workflow.completed
```

`_fail()` invoca la activity `update_expense_to_rejected` que escribe `status="rejected"` con `rejection_reason="VECTOR_SYNC_TIMEOUT"`. Como `gold.expense_audit` filtra `where status='approved'`, **la fila aprobada queda permanentemente excluida**.

### 3.2 Por qué `trigger_vector_sync` se quedaba sin tiempo

`trigger_vector_sync` polea `gold.expense_chunks` 18 veces × 60s buscando la fila del expense. Pero gold se materializa via DLT pipeline (cadencia ~10 min). Después de un HITL approve a las XX:43:00, el pipeline tiene que:

1. CDC propagar el `status=approved` a bronze (segundos)
2. Silver `apply_changes` (segundos, streaming)
3. Gold MV refresh (cuando el pipeline corra, hasta 10 min)
4. `gold.expense_chunks` materializar desde gold MV (próxima refresh)

En condiciones normales esto excede 18 min razonablemente seguido. Si el pipeline gold corre justo después del approve, ~5 min. Si corre justo antes, ~15 min para la próxima vuelta. **18 min es una ventana borderline**.

Y como BUG #2 paralizaba el CDC, los 18 min se hacían _infinitos_ — la fila nunca llegaba a silver, mucho menos a gold.

### 3.3 La filosofía del fix

El expense aprobado por humano es el _terminal state_ de negocio. Un missing embedding en el chat agent es un problema secundario. El código original mezclaba esos dos conceptos: trataba la falla del embedding como falla del expense.

**Fix**: separar las preocupaciones. Vector sync timeout queda como un evento observable (timeline + SSE) pero NO toca `expenses.status`. El workflow termina como `completed`. Cuando el medallion eventualmente alcanza, gold materializa la fila normalmente. Si quedó embedding pendiente, una retry out-of-band (la MERGE es idempotente) lo completa.

### 3.4 El fix

`expense_audit.py:243-318`:

```python
vec_result = await workflow.execute_activity("trigger_vector_sync", ...)
vec_status = (vec_result or {}).get("status")
if vec_status in ("failed", "timed_out_not_in_gold"):
    err_type = (vec_result or {}).get("error", vec_status)
    err_msg = (vec_result or {}).get("message", "...")
    # Emit timeline event para observabilidad
    await workflow.execute_activity("emit_expense_event", {
        "event_type": "vector_sync_failed",
        "details": {"status": vec_status, "error": err_type, "message": err_msg},
        ...
    })
    # SSE event
    await workflow.execute_activity("publish_event", {
        "event_type": "workflow.vector_sync_failed",
        ...
    })
    # NO hay self._fail aquí — caemos al workflow.completed normal

# 6) workflow.completed (siempre)
await workflow.execute_activity("publish_event", {"event_type": "workflow.completed", ...})
return {"status": "completed", "final_data": final_data}
```

`HITL_TIMEOUT` y `CANCELLED` siguen llamando `_fail` legítimamente — esos sí son fallas reales que deben rechazar el expense.

### 3.5 Recovery script para datos atrapados

`scripts/recover-vector-sync-rejected.js` es idempotente y filtra estrictamente:

```js
{
  status: "rejected",
  rejection_reason: { $in: ["VECTOR_SYNC_TIMEOUT", "VECTOR_SYNC_FAILED"] },
  final_amount: { $ne: null }   // proof que update_expense_to_approved sí corrió
}
```

`$set: { status: "approved" }`, `$unset: { rejection_reason: "" }`, `final_*` intactos. Agrega evento de timeline `actor.id="ops:recover-vector-sync-rejected"` para trazabilidad.

---

## 4. Capa 2: Debezium streaming — `ClassCastException String → byte[]`

### 4.1 Stack trace

```
ERROR [io.deb.emb.asy.AsyncEmbeddedEngine] Engine has failed with :
java.lang.ClassCastException: class java.lang.String cannot be cast to class [B
  at io.debezium.server.kafka.KafkaChangeConsumer.lambda$handleBatch$0(KafkaChangeConsumer.java:107)
  at org.apache.kafka.clients.producer.KafkaProducer.doSend(KafkaProducer.java:1106)
  at org.apache.kafka.clients.producer.KafkaProducer.send(KafkaProducer.java:991)
  at io.debezium.server.kafka.KafkaChangeConsumer.handleBatch(KafkaChangeConsumer.java:104)
```

Este crash ocurría en cada evento de **streaming** (`__op='u'/'d'`) pero NO en snapshots (`__op='r'`). Por eso bronze tenía sólo eventos snapshot — el connector siempre crasheaba al primer streaming.

### 4.2 La causa raíz

Debezium Server 3.0.0.Final tiene dos formatos JSON disponibles en `debezium-api-3.0.0.Final.jar`:

```bash
$ javap io.debezium.engine.format.Json
public class Json implements SerializationFormat<String> { ... }

$ javap io.debezium.engine.format.JsonByteArray
public class JsonByteArray implements SerializationFormat<byte[]> { ... }
```

La config tenía `debezium.format.value=json` → usaba `Json` → producía `String`.

**Pero algo en `KafkaChangeConsumer.lambda$handleBatch$0:107` hace `(byte[]) value` síncronamente**. Cuando `value` era `String` (snapshot _o_ streaming), el cast debería explotar SIEMPRE — pero solo lo hacía en streaming. ¿Por qué?

Hipótesis (no completamente confirmada por la falta de source jar): el path de _snapshot_ en Debezium Server pasa por `BatchSourceTask` con un converter chain que serializa Struct→byte[] _antes_ de llegar al sink. El path de _streaming_ usa `MongoDbStreamingChangeEventSource` que delega al format converter que para `Json` produce `String`. Cuando ese `String` llega al sink, `(byte[]) String` revienta.

Tu intuición fue clave: **"si el cast espera byte[], que reciba byte[] desde antes"**. La forma elegante: cambiar el format upstream para que produzca `byte[]` directamente.

### 4.3 Cómo se descubrió el nombre exacto del format

El nombre interno se deriva por reflexión en `DebeziumServer.<clinit>`:

```java
// Decompilado de debezium-server-core-3.0.0.Final
static {
    FORMAT_JSON              = Json.class.getSimpleName().toLowerCase();              // "json"
    FORMAT_JSON_BYTE_ARRAY   = JsonByteArray.class.getSimpleName().toLowerCase();     // "jsonbytearray"
    FORMAT_CLOUDEVENT        = CloudEvents.class.getSimpleName().toLowerCase();       // "cloudevents"
    FORMAT_AVRO              = Avro.class.getSimpleName().toLowerCase();              // "avro"
    FORMAT_PROTOBUF          = Protobuf.class.getSimpleName().toLowerCase();          // "protobuf"
    FORMAT_BINARY            = Binary.class.getSimpleName().toLowerCase();            // "binary"
    FORMAT_STRING            = SimpleString.class.getSimpleName().toLowerCase();      // "simplestring"
    FORMAT_CLIENT_PROVIDED   = ClientProvided.class.getSimpleName().toLowerCase();    // "clientprovided"
}
```

→ `debezium.format.value=jsonbytearray` (todo lowercase, sin separadores).

### 4.4 El fix

En `infra/terraform/debezium.tf`:

```hcl
{ name = "DEBEZIUM_FORMAT_VALUE", value = "jsonbytearray" },                                                             # antes "json"
{ name = "DEBEZIUM_FORMAT_KEY",   value = "jsonbytearray" },
{ name = "DEBEZIUM_SINK_KAFKA_PRODUCER_KEY_SERIALIZER",   value = "org.apache.kafka.common.serialization.ByteArraySerializer" },  # antes StringSerializer
{ name = "DEBEZIUM_SINK_KAFKA_PRODUCER_VALUE_SERIALIZER", value = "org.apache.kafka.common.serialization.ByteArraySerializer" },
```

### 4.5 Por qué bronze no necesita cambios

Spark Kafka source siempre entrega `value` como `binary`:

```python
# bronze/cdc_expenses.py:65
.load()
.filter(col("value").isNotNull())
.select(from_json(col("value").cast("string"), EXPENSES_JSON_SCHEMA).alias("p"))
```

El `.cast("string")` interpreta los bytes como UTF-8. Esto funcionaba antes (StringSerializer escribía bytes UTF-8 al hacer `getBytes()`) y funciona ahora (JsonByteArray ya escribe bytes UTF-8 directamente). **Bytes idénticos en Kafka, cero cambios en el medallion.**

### 4.6 La diferencia entre los dos paths

```
ANTES (rev:1-19) — format=json + StringSerializer:
  JsonFormat.serialize(struct) → String "{...}"
  StringSerializer.serialize(String) → byte[] (UTF-8 de "{...}")
  Kafka almacena: bytes UTF-8

  KafkaChangeConsumer:107: (byte[]) value
    snapshot path: value ya es byte[] aquí → cast OK
    streaming path: value es String → cast FALLA  ← BUG

DESPUÉS (rev:20+) — format=jsonbytearray + ByteArraySerializer:
  JsonByteArrayFormat.serialize(struct) → byte[] (UTF-8 de "{...}")
  ByteArraySerializer.serialize(byte[]) → mismos byte[] passthrough
  Kafka almacena: bytes UTF-8 idénticos

  KafkaChangeConsumer:107: (byte[]) value
    snapshot path: value es byte[] → cast OK
    streaming path: value es byte[] → cast OK  ← ARREGLADO
```

---

## 5. Capa 3: MSK — `auto.create.topics.enable=false`

### 5.1 Qué pasaba (después de arreglar Capa 2)

Con la Capa 2 arreglada el connector ya procesaba streaming events sin `ClassCastException`. Pero al primer evento de streaming sobre `chat_turns` (que el snapshot inicial captura aunque _no_ esté en `mongodb.collection.include.list` — quirk de Debezium MongoDB 3.0.0.Final), el producer intentaba publicar al topic `nexus.nexus_dev.chat_turns`. Ese topic _no existía_ y MSK no lo auto-creaba:

```
WARN [Producer] Error while fetching metadata: {nexus.nexus_dev.chat_turns=UNKNOWN_TOPIC_OR_PARTITION}
... [60 segundos de retries] ...
ERROR Failed to send record... TimeoutException: Topic nexus.nexus_dev.chat_turns not present in metadata after 60000 ms.
ERROR Engine has failed
```

El timeout mata el _engine entero_, no sólo ese envío. Como efecto colateral, todos los demás eventos válidos quedan en limbo.

### 5.2 Por qué `auto.create.topics.enable=false`

Era un default de seguridad en `aws_msk_configuration.nexus_prov`:

```hcl
server_properties = <<-EOT
  auto.create.topics.enable=false   # <-- bloqueaba el flow
  default.replication.factor=2
  ...
EOT
```

La política tradicional para producción es deshabilitar auto-create (evita typos creando topics garbage). Para CDC con Debezium en dev/staging, **necesitas auto-create** porque el connector descubre topics dinámicamente desde las collections de Mongo.

### 5.3 Por qué el `mongodb.collection.include.list` no salvó

Debezium MongoDB 3.0.0.Final tiene un quirk: el snapshot inicial captura **TODAS** las collections de la database (en nuestro caso 7), aunque el `collection.include.list` tenga sólo 5. También probamos `collection.exclude.list = chat_turns,chat_sessions` y también fue ignorado.

Es un bug conocido (no documentado claro) de esa versión. La forma confiable de evitar el crash sin pelear con ese filtro es dejar que MSK auto-cree los topics que no se usan.

### 5.4 El fix

En `infra/terraform/msk.tf`:

```hcl
server_properties = <<-EOT
  auto.create.topics.enable=true     # <-- cambio
  default.replication.factor=2
  min.insync.replicas=2
  num.partitions=3
  log.retention.hours=168
EOT
```

Update in-place del `aws_msk_configuration` + `aws_msk_cluster.configuration_info`. Rolling restart de brokers (~10-20 min). **NO replace del cluster, NO toca peering ni routes.**

### 5.5 Trade-off

Con auto-create habilitado, cualquier producer puede crear topics nuevos. En dev es aceptable. En prod, dos alternativas:
- Mantener `auto.create=false` y pre-crear los topics necesarios via Terraform (`aws_msk_topic` o terraform-provider-kafka).
- Migrar a Debezium 3.1+ donde `collection.include.list` parece respetarse en snapshot también (sin verificar).

---

## 6. Capa 4 (cosmético): Quarkus env-var mapping

### 6.1 El descubrimiento accidental

Durante el debugging probamos migrar de `FileOffsetBackingStore` a `KafkaOffsetBackingStore` para evitar re-snapshots cada vez que el connector se reiniciaba. Agregamos ~12 env vars del estilo:

```
DEBEZIUM_SOURCE_OFFSET_STORAGE                    = KafkaOffsetBackingStore
DEBEZIUM_SOURCE_OFFSET_STORAGE_TOPIC              = _debezium_offsets
DEBEZIUM_SOURCE_OFFSET_STORAGE_BOOTSTRAP_SERVERS  = b-1...,b-2...
DEBEZIUM_SOURCE_OFFSET_STORAGE_PRODUCER_SECURITY_PROTOCOL = SASL_SSL
... etc
```

**El connector falló a botear con `ConfigException: Invalid value null for configuration key.serializer`** — aunque `DEBEZIUM_SINK_KAFKA_PRODUCER_KEY_SERIALIZER` estaba presente en la task definition.

### 6.2 La causa probable

Quarkus + SmallRye Config tiene reglas no triviales para mapear env vars (`UPPER_SNAKE_CASE`) a properties (`lower.dotted.case`). Cuando hay env vars con sub-prefijos similares (`PRODUCER_*` apareciendo bajo `SINK_KAFKA_` y bajo `SOURCE_OFFSET_STORAGE_`), el mapping del `_` → `.` se hace ambiguo en propiedades de >5 niveles. El resultado fue que `key.serializer` no se reconoció pese a estar bien definido.

Verificación post-mortem: el TF dejó la variable `DEBEZIUM_SINK_KAFKA_PRODUCER_KEY_SERIALIZER` en el JSON del task definition (confirmado con `aws ecs describe-task-definition`), pero los logs del producer interno mostraban props todas vacías (`offset.storage.file.filename =`, `offset.storage.topic =`).

### 6.3 Workaround

Volvimos a `FileOffsetBackingStore` (más simple — sólo 2 env vars: storage class + filename) y los serializers volvieron a funcionar. Para eventualmente migrar a `KafkaOffsetBackingStore` la opción robusta es **bake un `application.properties` en la imagen Docker** (Debezium server lo lee directo de `/debezium/conf/application.properties` sin pasar por env→property mapping).

### 6.4 Variante del mismo problema con JAVA_OPTS

Como alternativa intentamos pasar las props via `-Ddebezium.source.offset.storage.X=Y` en `JAVA_OPTS`. Funcionó en parte pero el shell hace word-splitting de `JAVA_OPTS` antes de exec'r `java`. La prop `debezium.source.offset.storage.producer.sasl.jaas.config=software.amazon.msk.auth.iam.IAMLoginModule required;` tiene un **espacio en el valor** (`IAMLoginModule required;`), que el shell partió en dos args. Resultado: `java.lang.ClassNotFoundException: required;`.

Lección: SASL JAAS configs no se pueden pasar limpiamente vía JAVA_OPTS sin escape complejo. Properties files son lo correcto.

---

## 7. Diagnóstico — orden y herramientas

El diagnóstico tomó ~20 revisiones de task definition antes de llegar al fix. La trampa fue que **cada capa enmascaraba la siguiente**: arreglar una revelaba el bug debajo. Algunas técnicas que ayudaron:

### 7.1 Tres queries que revelan la inconsistencia

Cuando un evento "no llega a gold", ejecuta estas tres en este orden:

```javascript
// (1) Mongo — la fuente de verdad operacional
db.expenses.findOne({ expense_id: "exp_..." })
```

```sql
-- (2) Bronze — el feed CDC crudo
SELECT __op, status, _cdc_ingestion_ts
FROM nexus_dev.bronze.mongodb_cdc_expenses
WHERE expense_id = 'exp_...'
ORDER BY _cdc_ingestion_ts;
```

```sql
-- (3) Silver — el estado materializado
SELECT status, approved_at, updated_at
FROM nexus_dev.silver.expenses
WHERE expense_id = 'exp_...';
```

**Si Mongo y silver discrepan, el problema está en bronze (Debezium o MSK).** Si bronze sólo tiene `__op='r'`, Debezium no streamea. Si bronze tiene `__op='u'` pero silver no se actualiza, es la pipeline DLT.

### 7.2 CloudWatch insights útiles

```bash
# Patrones de error más frecuentes (categorizados)
aws logs filter-log-events --log-group-name /ecs/nexus-dev-edgm/debezium \
  --start-time $(date -v-24H +%s)000 \
  --filter-pattern "Nondeterminism" \
  --query 'events[].message' --output text | \
  grep -oE "scheduled event '[a-z_]+' .* '[a-z_]+'" | sort | uniq -c

# Ciclo de re-snapshot (si Debezium está en crash loop)
aws logs filter-log-events --log-group-name /ecs/nexus-dev-edgm/debezium \
  --filter-pattern "Finished snapshotting" \
  --query 'events[].timestamp' --output text | wc -l
# Si ves >3 snapshots por hora, hay crash loop — el offset storage es ephemeral
```

### 7.3 Inspeccionar la imagen Docker localmente

Sin internet no podías encontrar la lista exacta de `format.value` válidos. Pero `docker run --rm --entrypoint sh debezium/server:3.0.0.Final` te deja decompilar:

```bash
docker run --rm --entrypoint sh debezium/server:3.0.0.Final -c '
  mkdir -p /tmp/x && cd /tmp/x
  jar xf /debezium/lib/debezium-server-core-3.0.0.Final.jar
  javap -v -p io.debezium.server.DebeziumServer
' | awk "/static \{\};/,/^[ ]+private/" | head -80
```

Esto reveló el switch case que mapea `format.value` strings a clases — exactamente lo que necesitábamos saber para descubrir `jsonbytearray`.

### 7.4 Cuando el bug "se mueve" en lugar de arreglarse

Si arreglas algo y el error nuevo es **diferente al anterior**, es buena señal — capas se están destapando. Lleva un journal:

```
rev:9   StringSerializer + format=json     → ClassCastException @KafkaConsumer:107 (streaming)
rev:11  removí serializers                  → ConfigException: key.serializer null
rev:12  rollback, JAVA_OPTS w/ Memory store → JAVA_OPTS funciona
rev:15  JAVA_OPTS w/ Kafka store + JAAS    → ClassNotFoundException: 'required;' (word splitting)
rev:16  Memory store solamente              → ClassCastException sigue
rev:17  sin SMT                             → ClassCastException sigue (SMT no era la causa)
rev:19  jsonbytearray + ByteArraySerializer → CAST ARREGLADO, pero TimeoutException @ chat_turns
rev:20  exclude.list                        → ignorado por Debezium 3.0
+MSK    auto.create.topics.enable=true      → topics se crean → all green
```

Cada paso te dice algo nuevo del sistema. Mantener este registro en un md durante el debugging es lo que permitió no perderse.

---

## 8. Lecciones aprendidas

### 8.1 Técnicas

**L1. Errores enmascarados son la regla, no la excepción.** Un bug visible al usuario suele ser la cima de un iceberg de bugs más profundos. Cuando arreglas el visible, mira si aparecen nuevos errores antes de declarar éxito. El "todo OK" puede significar "estás procesando menos cosas y por eso no alcanzas el siguiente bug".

**L2. Cuando el dato fluye, audita las cuatro capas en orden:** Mongo → bronze (CDC raw) → silver (materialización) → gold (filtro de business). Las tres queries de §7.1 revelan en cuál capa se rompe el flujo. Saltarse una lleva a diagnosticar el síntoma equivocado.

**L3. Operaciones idempotentes en el workflow ahorran horas de recovery.** El MERGE de `gold.expense_embeddings` es idempotente, eso permitió que mi fix simplemente _no_ rechace el expense y deje el embedding pendiente — sin reintentos complicados, sin estado intermedio.

**L4. `_fail()` debe rechazar el _expense_ sólo si el _expense_ es inválido.** Las fallas de orquestación (vector_sync, embedding, propagación) son ortogonales al estado de negocio. Mezclarlas significa que un retry del observability infra rompe transacciones del negocio.

**L5. Caster (Kafka producer's `(byte[]) value`) confía en quien le entrega el dato.** Si dos pipelines (snapshot vs streaming) entregan tipos diferentes al mismo cast, _uno_ va a explotar. La solución correcta es alinear los productores, no parchear el cast.

**L6. Fuente de verdad sobre nombres internos: `javap`.** Cuando documentación falta o está incorrecta, decompilar el bytecode da el nombre exacto que el código espera. Inspeccionar `<clinit>` revela cómo se construyen las constantes.

**L7. Quarkus/SmallRye env→property mapping no es 1:1 obvio.** Para configs críticas (Kafka serializers, JAAS configs con espacios), preferir `application.properties` bakeado en la imagen sobre env vars. El env var resolution en propiedades anidadas profundas tiene quirks no documentados.

**L8. Defensa en profundidad para infra crítica.** El pattern de `prevent_destroy = true` + EventBridge auto-heal Lambda + CloudTrail para route que estaba documentado en `route_self_heal.tf` salvó múltiples applies durante el incidente. Replicar ese pattern para otros assets críticos (peering, NAT, security groups que controlan el bronze pipeline).

### 8.2 Proceso

**P1. Limita el blast radius con `-target` en TF apply** cuando el state tiene historial de regresiones. La doc de HashiCorp dice "use as a last resort" — pero para debugging iterativo en un state con bugs conocidos, es exactamente apropiado.

**P2. Snapshot _antes y después_ de cada apply los recursos críticos.** En este caso, `aws ec2 describe-route-tables` antes/después confirmó que ningún apply tocó el peering. Sin esto, asumirías que no se tocó cuando talvez sí.

**P3. Cada hipótesis debe ser testeable con el mínimo cambio.** Cuando estábamos perdidos sin saber si el SMT era el culpable, probamos quitarlo entero (rev:17). Resultado: el bug persistió → SMT NO era la causa. Esto eliminó la rama entera del árbol de hipótesis con un solo apply.

**P4. Reversiones explícitas y nombradas.** Cuando un cambio empeora las cosas, no lo dejes. Revierte _explícitamente_ con un commit/edit que lo declare ("rev:18 — restaurando SMT, sin él silver/gold se rompen"). Esto evita que un colaborador futuro herede tu estado intermedio creyendo que es intencional.

**P5. Cuando un bug es de versión, busca los release notes _y_ los issues abiertos.** En este caso, las release notes de Debezium 3.1+ no mencionaban ClassCastException explícitamente, pero buscar en Google Groups encontró un caso idéntico (`Cannot add partition to transaction before completing initTransactions` enmascarado como ClassCast). Pista importante que llevó a `enable.idempotence=false` (que aunque no fue el fix definitivo, ayudó a entender el dominio del bug).

**P6. Persiste el journal del incidente.** El árbol de hipótesis (§7.4) probadas y descartadas es info de oro para futuros debuggings. Sin esa cadena, no hay forma de saber por qué la solución obvia (`format=json`) no funciona — alguien la volverá a intentar.

### 8.3 Arquitectura

**A1. CDC en producción NECESITA offset storage durable** (Kafka offset store, no FileOffsetBackingStore en Fargate). El comment ya lo decía en `debezium.tf`: "Aceptable en dev con snapshot.mode=initial. Para prod migrar a KafkaOffsetBackingStore". Pero la migración tiene complicaciones (Quarkus mapping en este caso) — no la dejes para "prod-ready" sin haberla probado en dev.

**A2. Source filter (Debezium) ≠ Sink filter (MSK).** Si el source captura más de lo que necesitas y el sink no auto-crea topics, el sink se vuelve un single-point-of-failure para el source. Tres opciones:
- Filtrar correctamente en el source (no funcionó con MongoDB connector 3.0).
- Pre-crear los topics que necesita el source (más infra, más control).
- Auto-create en el sink (lo que hicimos, simple pero crea topics garbage).

**A3. Vector embedding pipeline debería ser asíncrono al workflow.** Hoy `trigger_vector_sync` bloquea el workflow durante 18 min de polling. Si ese workflow falla, contamina el estado del expense (bug original). Mejor patrón:
- Workflow termina rápido, marca expense como `approved`.
- Worker separado consume del topic gold (o un Mongo CDC) y hace el embedding sin bloquear.
- Retries idempotentes naturales.

**A4. MV refresh schedule es parte del SLA de gold.** Si gold se materializa cada 10 min, ningún consumidor que polee gold por menos de 20 min es robusto. Documentarlo o cambiar a streaming refresh.

---

## 9. Verificación end-to-end post-fix

Cuando puedas verificar en Databricks:

```sql
-- (1) Bronze tiene eventos de update post-fix?
SELECT __op, status, expense_id, _cdc_ingestion_ts
FROM nexus_dev.bronze.mongodb_cdc_expenses
WHERE expense_id = 'exp_01KQ3HRW5GVTWNTT34KNPD7FN1'
ORDER BY _cdc_ingestion_ts DESC LIMIT 10;
-- Esperado: __op='u' con status='approved' (resultado del recovery script)

-- (2) Silver convergió?
SELECT status, final_amount, final_currency, approved_at, updated_at
FROM nexus_dev.silver.expenses
WHERE expense_id = 'exp_01KQ3HRW5GVTWNTT34KNPD7FN1';
-- Esperado: status='approved', final_amount=145500, final_currency='COP'

-- (3) Gold lo recogió?
SELECT expense_id, final_amount, final_currency, final_vendor, had_hitl, hitl_decision
FROM nexus_dev.gold.expense_audit
WHERE expense_id = 'exp_01KQ3HRW5GVTWNTT34KNPD7FN1';
-- Esperado: una fila con final_amount=145500, had_hitl=true (después del próximo refresh)
```

Para validar el flujo HITL nuevo end-to-end (después de deploy del worker con el fix):

1. Upload de un receipt con discrepancia → HITL modal aparece
2. Aprobar HITL en frontend
3. Esperar al menos un ciclo del DLT pipeline gold (~10 min)
4. Confirmar query (3) arriba retorna fila

Si vector_sync hace timeout en ese run, verás un evento `vector_sync_failed` en la timeline pero el expense termina `approved` igual y aparece en gold.

---

## Apéndice: Línea de tiempo del incidente y fix

```
2026-04-25 ~22:00  Usuario reporta "HITL approve no aparece en gold"
            ~22:30  Diagnóstico inicial — recovery script + fix workflow listo
2026-04-26 02:11   Inicio del rabbit hole de Debezium ClassCastException
            02:31  Primer intento (KafkaOffsetBackingStore) — connector no bootea
            02:36  Restauro serializers, KafkaOffsetBackingStore — descubierto que
                   key.serializer "no se mapea" desde env var
            02:40  Memory store via JAVA_OPTS — bootea, snapshots OK, streaming crashea
            02:55  Quito SMT — streaming sigue crasheando (descarta SMT como causa)
            03:10  Descubierto JsonByteArray en debezium-api jar via javap
            03:11  Apply rev:19 con jsonbytearray + ByteArraySerializer — CAST ARREGLADO
            03:13  Streaming activo pero crashea en chat_turns timeout
            03:16  exclude.list ignorado por Debezium 3.0 — confirmado
            03:18  MSK config update auto.create.topics.enable=true — apply iniciado
            03:28  MSK rolling broker update completo
            03:29  Force redeploy Debezium task — bootea, snapshot, streaming, ESTABLE
            03:30+ Sin ClassCastException, sin UNKNOWN_TOPIC, sin Engine has failed
                   por >2 minutos consecutivos con 5 updates de prueba procesados
```

**~5 horas en total**, ~20 task definition revisions, fix definitivo en 4 capas. El fix de la capa visible al usuario (workflow) tomó 30 minutos. El resto fue desentrañar las capas debajo que enmascaraban el problema real del CDC.
