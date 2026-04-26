# ── Phase B+ · Debezium Server (Mongo change streams → MSK) ──────────
#
# Debezium Server 3.x corre como imagen oficial debezium/server:3.0.0.Final
# en ECS Fargate. Todas las props del connector se pasan via env vars
# (prefijo DEBEZIUM_*), no hay volumen de config.
#
# El task reutiliza:
#   - aws_ecs_cluster.main
#   - aws_iam_role.ecs_task_execution (resuelve secrets y pull ECR)
#   - aws_secretsmanager_secret.mongodb_uri
#   - aws_security_group.worker (egress-only a Mongo + MSK)
#   - aws_subnet.private
#
# Se crean nuevos: task role con permisos MSK, log group, task def,
# service. Gateado por var.msk_enabled (Debezium requiere MSK para
# publicar) + desired_count para encenderlo solo cuando queremos.

variable "debezium_image" {
  description = "Imagen Docker de Debezium Server con aws-msk-iam-auth jar añadido. Buildeada desde debezium-image/Dockerfile."
  type        = string
  default     = "525237381234.dkr.ecr.us-east-1.amazonaws.com/nexus-dev-edgm-debezium:3.0.0-msk-iam-v5"
}

variable "debezium_desired_count" {
  description = "0 hasta que MSK esté arriba y queramos arrancar el productor."
  type        = number
  default     = 0
}

# ── CloudWatch log group ─────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "debezium" {
  count             = var.msk_enabled ? 1 : 0
  name              = "/ecs/${var.prefix}/debezium"
  retention_in_days = 14
}

# ── IAM task role — permisos MSK + Mongo secret ──────────────────────
# kafka-cluster:* se scopea al ARN del cluster; la wildcard sobre
# topic/group usa el pattern documentado por AWS para IAM auth de MSK.

resource "aws_iam_role" "debezium_task" {
  count              = var.msk_enabled ? 1 : 0
  name               = "${var.prefix}-ecs-task-debezium"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume.json
}

data "aws_iam_policy_document" "debezium_task" {
  count = var.msk_enabled ? 1 : 0

  # El role necesita acceso a ambos clusters durante la migración:
  # apunta al Serverless hasta el cutover, después al Provisioned. Los
  # ARNs se combinan dinámicamente para que un solo apply funcione
  # ANTES y DESPUÉS del flip de msk_prefer_provisioned.
  statement {
    sid     = "MSKConnect"
    effect  = "Allow"
    actions = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = compact([
      var.msk_enabled ? aws_msk_serverless_cluster.nexus[0].arn : "",
      var.msk_provisioned_enabled ? aws_msk_cluster.nexus_prov[0].arn : "",
    ])
  }

  statement {
    sid    = "MSKTopicWrite"
    effect = "Allow"
    actions = [
      "kafka-cluster:CreateTopic",
      "kafka-cluster:DescribeTopic",
      "kafka-cluster:WriteData",
      "kafka-cluster:ReadData",
      "kafka-cluster:DescribeTopicDynamicConfiguration",
      "kafka-cluster:AlterTopicDynamicConfiguration",
    ]
    resources = compact([
      var.msk_enabled ? "${replace(aws_msk_serverless_cluster.nexus[0].arn, ":cluster/", ":topic/")}/*" : "",
      var.msk_provisioned_enabled ? "${replace(aws_msk_cluster.nexus_prov[0].arn, ":cluster/", ":topic/")}/*" : "",
    ])
  }

  statement {
    sid    = "MSKGroupRead"
    effect = "Allow"
    actions = [
      "kafka-cluster:AlterGroup",
      "kafka-cluster:DescribeGroup",
    ]
    resources = compact([
      var.msk_enabled ? "${replace(aws_msk_serverless_cluster.nexus[0].arn, ":cluster/", ":group/")}/*" : "",
      var.msk_provisioned_enabled ? "${replace(aws_msk_cluster.nexus_prov[0].arn, ":cluster/", ":group/")}/*" : "",
    ])
  }
}

resource "aws_iam_role_policy" "debezium_task" {
  count  = var.msk_enabled ? 1 : 0
  name   = "${var.prefix}-ecs-task-debezium"
  role   = aws_iam_role.debezium_task[0].id
  policy = data.aws_iam_policy_document.debezium_task[0].json
}

# ── Task definition ──────────────────────────────────────────────────
# Debezium Server en Fargate: 1 vCPU / 2 GB. Java agent es más pesado
# que el listener Python — confirmado por el plan doc. Si aparece GC
# pressure en logs, subir a 2048/4096.

resource "aws_ecs_task_definition" "debezium" {
  count                    = var.msk_enabled ? 1 : 0
  family                   = "${var.prefix}-debezium"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.debezium_task[0].arn

  container_definitions = jsonencode([{
    name      = "debezium"
    image     = var.debezium_image
    essential = true

    environment = [
      # ── Source: MongoDB ────────────────────────────────────────────
      { name = "DEBEZIUM_SOURCE_CONNECTOR_CLASS", value = "io.debezium.connector.mongodb.MongoDbConnector" },
      { name = "DEBEZIUM_SOURCE_TOPIC_PREFIX", value = "nexus" },
      { name = "DEBEZIUM_SOURCE_MONGODB_DATABASE_INCLUDE_LIST", value = "nexus_dev" },
      { name = "DEBEZIUM_SOURCE_MONGODB_COLLECTION_INCLUDE_LIST",
      value = "nexus_dev.expenses,nexus_dev.receipts,nexus_dev.hitl_tasks,nexus_dev.ocr_extractions,nexus_dev.expense_events" },
      # Exclude explícito: el connector MongoDB de Debezium 3.0.0.Final
      # tiende a snapshotear TODAS las collections de la database aunque
      # `collection.include.list` esté seteado. Cuando intenta publicar
      # `chat_turns`/`chat_sessions` al topic correspondiente y el topic
      # no existe en MSK (auto.create no está garantizado), el producer
      # hace timeout 60s y crashea el engine. Exclusión explícita evita
      # que el snapshot/streaming las toque del todo.
      { name = "DEBEZIUM_SOURCE_MONGODB_COLLECTION_EXCLUDE_LIST",
      value = "nexus_dev.chat_turns,nexus_dev.chat_sessions" },
      # capture.mode: full document en update; pre-images se habilitan via
      # collMod changeStreamPreAndPostImages en Mongo (ver runbook).
      # NO existe 'change_streams_update_full_with_pre_image' como valor
      # de capture.mode — el preaje se gatea server-side.
      { name = "DEBEZIUM_SOURCE_CAPTURE_MODE", value = "change_streams_update_full" },
      { name = "DEBEZIUM_SOURCE_SNAPSHOT_MODE", value = "initial" },

      # ── Offset storage ─────────────────────────────────────────────
      # MemoryOffsetBackingStore via JAVA_OPTS. Ephemeral (cada restart
      # re-snapshot las 7 collections en ~5s), pero con la imagen 3.1.1
      # el connector ya no crashea en streaming, así que la task se queda
      # estable y el re-snapshot ocurre solo en deploys/OOM/scaling.
      # Migrar a Kafka offset store es la próxima mejora — bloqueado por
      # un quirk de Quarkus/SmallRye que no mapea
      # `DEBEZIUM_SOURCE_OFFSET_STORAGE_*` env vars a las props internas
      # del connector (síntoma documentado en rev:11–13).
      { name = "JAVA_OPTS", value = "-Ddebezium.source.offset.storage=org.apache.kafka.connect.storage.MemoryOffsetBackingStore" },
      { name = "DEBEZIUM_SOURCE_OFFSET_FLUSH_INTERVAL_MS", value = "10000" },

      # ── SMT: unwrap + append __op / __source_ts_ms ─────────────────
      # Verificado 2026-04-26: el ClassCastException de streaming NO viene
      # del SMT (lo deshabilitamos en rev:17 y el bug persistió igual).
      # Está en el path KafkaChangeConsumer→KafkaProducer.doSend de
      # Debezium Server 3.0.0.Final. Restaurando el SMT porque sin él
      # bronze/silver/gold no entienden el envelope Debezium standard.
      { name = "DEBEZIUM_TRANSFORMS", value = "unwrap" },
      { name = "DEBEZIUM_TRANSFORMS_UNWRAP_TYPE", value = "io.debezium.connector.mongodb.transforms.ExtractNewDocumentState" },
      { name = "DEBEZIUM_TRANSFORMS_UNWRAP_ADD_FIELDS", value = "op,source.ts_ms" },
      { name = "DEBEZIUM_TRANSFORMS_UNWRAP_DELETE_TOMBSTONE_HANDLING_MODE", value = "rewrite" },
      { name = "DEBEZIUM_TRANSFORMS_UNWRAP_ARRAY_ENCODING", value = "document" },

      # ── Sink: Kafka / MSK IAM ──────────────────────────────────────
      { name = "DEBEZIUM_SINK_TYPE", value = "kafka" },
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_BOOTSTRAP_SERVERS", value = local.msk_bootstrap_active },
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_SECURITY_PROTOCOL", value = "SASL_SSL" },
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_SASL_MECHANISM", value = "AWS_MSK_IAM" },
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_SASL_JAAS_CONFIG", value = "software.amazon.msk.auth.iam.IAMLoginModule required;" },
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_SASL_CLIENT_CALLBACK_HANDLER_CLASS", value = "software.amazon.msk.auth.iam.IAMClientCallbackHandler" },
      # JSON format. value.serializer/key.serializer SON OBLIGATORIOS
      # en Debezium Server 3.0.0.Final — el `KafkaChangeConsumer.start()`
      # construye el KafkaProducer pasándole los `debezium.sink.kafka.producer.*`
      # raw, sin auto-derivar nada del `format`. Si faltan, falla en boot
      # con `ConfigException: Invalid value null for configuration key.serializer`
      # (visto en task def revisión :10).
      #
      # El ClassCastException original (`String cannot be cast to [B` en
      # KafkaChangeConsumer.java:107 → KafkaProducer.doSend:1106) NO viene
      # de aquí — la combinación `format=json` (JsonFormat → String) +
      # `StringSerializer` permitía que los snapshots iniciales (`__op='r'`)
      # se publicaran sin problema. La crash sucede sólo al pasar de
      # snapshot a streaming. Hipótesis vigente: bug en Debezium Server
      # 3.0.0.Final con la combinación de SMTs (`array.encoding=document`
      # + `delete.tombstone.handling.mode=rewrite` + `add.fields=op,source.ts_ms`)
      # cuando procesan eventos `__op='u'/'d'` en lugar de `'r'`. Fix
      # definitivo probable: subir el base image a `debezium/server:3.0.5.Final`
      # o más nuevo en `debezium-image/Dockerfile`.
      # Serializers byte[] end-to-end. La causa raíz del
      # `ClassCastException: String cannot be cast to [B` que crasheaba
      # cada evento de streaming es que algo en el path
      # KafkaChangeConsumer→KafkaProducer.doSend hace `(byte[]) value`
      # SÍNCRONAMENTE. Con `format=json` (`io.debezium.engine.format.Json`
      # implements `SerializationFormat<String>` — verificado con javap)
      # el valor llega como String, el cast explota. Snapshots no
      # disparan ese path por razones internas y por eso publicaban OK.
      #
      # Fix: usar `jsonbytearray` (nombre derivado de
      # `JsonByteArray.class.getSimpleName().toLowerCase()` per
      # DebeziumServer.java static block) — implementa
      # `SerializationFormat<byte[]>` así que el value llega como
      # byte[] y el cast succeed. Pareado con `ByteArraySerializer`
      # — el producer envía bytes nativos a Kafka.
      #
      # Bronze sigue compatible: hace `from_json(col("value").cast("string"))`
      # y `byte[].cast("string")` en Spark deserializa UTF-8 trivialmente.
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_KEY_SERIALIZER", value = "org.apache.kafka.common.serialization.ByteArraySerializer" },
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_VALUE_SERIALIZER", value = "org.apache.kafka.common.serialization.ByteArraySerializer" },
      # Mantenemos enable.idempotence=false como defensa adicional —
      # Kafka 3.x lo activa por default y el flujo transaccional implícito
      # con SASL_SSL/MSK IAM puede agregar latencia/quirks. No daña tener
      # esto explícito.
      { name = "DEBEZIUM_SINK_KAFKA_PRODUCER_ENABLE_IDEMPOTENCE", value = "false" },
      { name = "DEBEZIUM_FORMAT_VALUE", value = "jsonbytearray" },
      { name = "DEBEZIUM_FORMAT_KEY", value = "jsonbytearray" },
      { name = "DEBEZIUM_FORMAT_VALUE_SCHEMAS_ENABLE", value = "false" },
      { name = "DEBEZIUM_FORMAT_KEY_SCHEMAS_ENABLE", value = "false" },

      # ── Logging ────────────────────────────────────────────────────
      { name = "QUARKUS_LOG_LEVEL", value = "INFO" },
      { name = "QUARKUS_LOG_CONSOLE_JSON", value = "false" },
    ]

    secrets = [
      { name = "DEBEZIUM_SOURCE_MONGODB_CONNECTION_STRING", valueFrom = aws_secretsmanager_secret.mongodb_uri.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.debezium[0].name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

# ── Service ──────────────────────────────────────────────────────────

resource "aws_ecs_service" "debezium" {
  count                              = var.msk_enabled ? 1 : 0
  name                               = "${var.prefix}-debezium"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.debezium[0].arn
  launch_type                        = "FARGATE"
  desired_count                      = var.debezium_desired_count
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.worker.id]
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}

# ── ECR repo para imagen custom de Debezium con aws-msk-iam-auth jar ─

resource "aws_ecr_repository" "debezium" {
  count                = var.msk_enabled ? 1 : 0
  name                 = "${var.prefix}-debezium"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "debezium" {
  count      = var.msk_enabled ? 1 : 0
  repository = aws_ecr_repository.debezium[0].name
  policy     = local.ecr_lifecycle_policy
}

output "debezium_ecr_url" {
  value = var.msk_enabled ? aws_ecr_repository.debezium[0].repository_url : ""
}
