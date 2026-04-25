#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────
# Observa eventos de Kafka (MSK Provisioned) sin abrir la red al público.
#
# Lanza una task Fargate one-shot en el VPC que corre kafka-console-consumer
# con IAM SASL auth. Los mensajes van a CloudWatch, y el script te los tail-ea
# desde tu terminal hasta que el task expira (default: 5 min).
#
# Uso:
#   scripts/kafka-watch.sh                        # default: expenses, 5 min, latest
#   scripts/kafka-watch.sh receipts
#   scripts/kafka-watch.sh expenses --beginning
#   scripts/kafka-watch.sh hitl_tasks --duration=600
#   scripts/kafka-watch.sh all                    # 5 topics en paralelo
#
#   scripts/kafka-watch.sh create-topics nexus.nexus_dev.expenses \
#     nexus.nexus_dev.receipts ... [--partitions=3] [--replication-factor=2]
#
# Argumentos:
#   $1  = verbo: create-topics, o topic short name (expenses|receipts|...|dlq|all)
#         o el nombre completo si empieza con "nexus."
#   --beginning  lee desde el primer mensaje del topic (default: latest)
#   --duration=N segundos que vive la task (default: 300 = 5 min, max 3600)
#   --bootstrap=HOST:PORT   override del bootstrap (default: provisioned)
#   --partitions=N           (solo create-topics, default: 3)
#   --replication-factor=N   (solo create-topics, default: 2)
#
# Requisitos:
#   - AWS CLI con credenciales del account 525237381234
#   - Acceso al cluster ECS nexus-dev-edgm y a /ecs/nexus-dev-edgm/debezium
# ────────────────────────────────────────────────────────────────────

set -euo pipefail

CLUSTER="nexus-dev-edgm"
PREFIX="nexus-dev-edgm"
LOG_GROUP="/ecs/nexus-dev-edgm/debezium"
BOOTSTRAP="b-1.nexusdevedgmkafkaprov.ygf4z6.c23.kafka.us-east-1.amazonaws.com:9098,b-2.nexusdevedgmkafkaprov.ygf4z6.c23.kafka.us-east-1.amazonaws.com:9098"
MSK_IAM_VERSION="2.3.5"

# Defaults
MODE="tail"   # tail | create-topics
TOPIC_ARG=""
OFFSET_MODE="latest"
DURATION=300
PARTITIONS=3
REPLICATION_FACTOR=2
BOOTSTRAP_OVERRIDE=""
CREATE_TOPICS=()

# Primer arg puede ser el verbo "create-topics"; si no, asumimos "tail".
if [ "${1:-}" = "create-topics" ]; then
  MODE="create-topics"
  shift
fi

# Parse all args uniformly (handle --help first, then topic, then flags)
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      sed -n '1,25p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    --beginning|--from-beginning) OFFSET_MODE="beginning" ;;
    --duration=*)                 DURATION="${arg#--duration=}" ;;
    --partitions=*)               PARTITIONS="${arg#--partitions=}" ;;
    --replication-factor=*)       REPLICATION_FACTOR="${arg#--replication-factor=}" ;;
    --bootstrap=*)                BOOTSTRAP_OVERRIDE="${arg#--bootstrap=}" ;;
    --*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *)
      if [ "$MODE" = "create-topics" ]; then
        CREATE_TOPICS+=("$arg")
      elif [ -z "$TOPIC_ARG" ]; then
        TOPIC_ARG="$arg"
      else
        echo "extra positional arg ignored: $arg" >&2
      fi
      ;;
  esac
done
TOPIC_ARG="${TOPIC_ARG:-expenses}"

# Override del bootstrap (por si se quiere apuntar a otro cluster ad-hoc).
if [ -n "$BOOTSTRAP_OVERRIDE" ]; then
  BOOTSTRAP="$BOOTSTRAP_OVERRIDE"
fi

if [ "$MODE" = "create-topics" ] && [ ${#CREATE_TOPICS[@]} -eq 0 ]; then
  echo "ERROR: create-topics requiere al menos un nombre de topic" >&2
  echo "Uso: scripts/kafka-watch.sh create-topics <topic1> [<topic2>...] [--partitions=N] [--replication-factor=N]" >&2
  exit 2
fi

# Clamp duration
if [ "$DURATION" -lt 60 ];   then DURATION=60; fi
if [ "$DURATION" -gt 3600 ]; then DURATION=3600; fi

# ── Resolve topic name(s) ────────────────────────────────────────────
topic_full() {
  case "$1" in
    nexus.*) echo "$1" ;;
    dlq)     echo "nexus.dlq" ;;
    *)       echo "nexus.nexus_dev.$1" ;;
  esac
}

if [ "$TOPIC_ARG" = "all" ]; then
  TOPICS=(
    "nexus.nexus_dev.expenses"
    "nexus.nexus_dev.receipts"
    "nexus.nexus_dev.hitl_tasks"
    "nexus.nexus_dev.ocr_extractions"
    "nexus.nexus_dev.expense_events"
  )
else
  TOPICS=("$(topic_full "$TOPIC_ARG")")
fi

# ── Resolve VPC bits from terraform outputs (fallback a hardcoded) ───
cd_to_repo_root() {
  local d="$PWD"
  while [ "$d" != "/" ]; do
    [ -d "$d/.git" ] && { echo "$d"; return; }
    d="$(dirname "$d")"
  done
  echo "$PWD"
}
REPO="$(cd_to_repo_root)"
TF_DIR="$REPO/infra/terraform"

resolve_subnets_sg() {
  local subnets="" sg=""
  if [ -d "$TF_DIR" ]; then
    subnets=$(cd "$TF_DIR" && terraform output -json private_subnet_ids 2>/dev/null | jq -r 'join(",")' 2>/dev/null || true)
  fi
  sg=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${PREFIX}-worker-sg" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)

  if [ -z "$subnets" ] || [ "$subnets" = "null" ]; then
    subnets="subnet-0c3e84042c54f5ed7,subnet-0b89dcd7a6e5e0e70"
  fi
  if [ -z "$sg" ] || [ "$sg" = "None" ]; then
    sg="sg-0895ebcd6eccd6191"
  fi
  echo "${subnets}|${sg}"
}
RES="$(resolve_subnets_sg)"
SUBNETS="${RES%|*}"
SG="${RES#*|}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# ── Build consumer command ────────────────────────────────────────────
# Comillas cuidadas para que se ejecute como un solo comando bash dentro
# del container.
OFFSET_FLAG=""
if [ "$OFFSET_MODE" = "beginning" ]; then
  OFFSET_FLAG="--from-beginning"
fi

# Para "all" usamos kafka-console-consumer con --include regex (consume N topics).
if [ "$TOPIC_ARG" = "all" ]; then
  TOPIC_FLAG="--include nexus\\\\.nexus_dev\\\\..*"
else
  TOPIC_FLAG="--topic ${TOPICS[0]}"
fi

if [ "$MODE" = "create-topics" ]; then
  # Para create-topics armamos un script bash que corre kafka-topics.sh
  # --create por cada topic (idempotente con --if-not-exists).
  TOPIC_CREATE_CMDS=""
  for t in "${CREATE_TOPICS[@]}"; do
    TOPIC_CREATE_CMDS+=$'\n'"echo '=== creating topic: ${t}'"$'\n'
    TOPIC_CREATE_CMDS+="/opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server ${BOOTSTRAP} --command-config /tmp/c.properties --create --if-not-exists --topic ${t} --partitions ${PARTITIONS} --replication-factor ${REPLICATION_FACTOR} || true"
  done
  # También listamos al final para confirmar.
  TOPIC_CREATE_CMDS+=$'\n'"echo '=== topics after create:'"$'\n'
  TOPIC_CREATE_CMDS+="/opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server ${BOOTSTRAP} --command-config /tmp/c.properties --list"

  CMD=$(cat <<EOF
set -e
cd /tmp
cat > c.properties <<CFG
security.protocol=SASL_SSL
sasl.mechanism=AWS_MSK_IAM
sasl.jaas.config=software.amazon.msk.auth.iam.IAMLoginModule required;
sasl.client.callback.handler.class=software.amazon.msk.auth.iam.IAMClientCallbackHandler
CFG
curl -sL -o /opt/bitnami/kafka/libs/aws-msk-iam-auth.jar \
  https://github.com/aws/aws-msk-iam-auth/releases/download/v${MSK_IAM_VERSION}/aws-msk-iam-auth-${MSK_IAM_VERSION}-all.jar
echo "=== kafka-watch create-topics starting at \$(date -u) ==="
echo "=== bootstrap: ${BOOTSTRAP}"
echo "=== topics:    ${CREATE_TOPICS[*]}"
echo "=== partitions=${PARTITIONS} replication=${REPLICATION_FACTOR}"
echo "==="
${TOPIC_CREATE_CMDS}
echo "=== kafka-watch create-topics done at \$(date -u) ==="
EOF
  )
else
  CMD=$(cat <<EOF
set -e
cd /tmp
cat > c.properties <<CFG
security.protocol=SASL_SSL
sasl.mechanism=AWS_MSK_IAM
sasl.jaas.config=software.amazon.msk.auth.iam.IAMLoginModule required;
sasl.client.callback.handler.class=software.amazon.msk.auth.iam.IAMClientCallbackHandler
CFG
curl -sL -o /opt/bitnami/kafka/libs/aws-msk-iam-auth.jar \
  https://github.com/aws/aws-msk-iam-auth/releases/download/v${MSK_IAM_VERSION}/aws-msk-iam-auth-${MSK_IAM_VERSION}-all.jar
echo "=== kafka-watch starting at \$(date -u) ==="
echo "=== bootstrap: ${BOOTSTRAP}"
echo "=== topic:     ${TOPICS[*]}"
echo "=== offset:    ${OFFSET_MODE}"
echo "=== timeout:   ${DURATION}s"
echo "==="
/opt/bitnami/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server ${BOOTSTRAP} \
  --consumer.config /tmp/c.properties \
  ${TOPIC_FLAG} \
  ${OFFSET_FLAG} \
  --timeout-ms $((DURATION * 1000)) \
  --property print.timestamp=true \
  --property print.key=true \
  --property print.partition=true \
  --property key.separator=' | '
echo "=== kafka-watch done at \$(date -u) ==="
EOF
  )
fi

# ── Register task definition (idempotente — ECS reusa si no cambió) ──
TD_JSON=$(cat <<EOF
{
  "family": "${PREFIX}-kafka-watch",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::${ACCOUNT}:role/${PREFIX}-ecs-task-execution",
  "taskRoleArn": "arn:aws:iam::${ACCOUNT}:role/${PREFIX}-ecs-task-debezium",
  "containerDefinitions": [{
    "name": "watcher",
    "image": "public.ecr.aws/bitnami/kafka:3.7",
    "essential": true,
    "entryPoint": ["/bin/bash","-c"],
    "command": [$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<<"$CMD")],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "${LOG_GROUP}",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "kafka-watch"
      }
    }
  }]
}
EOF
)

echo "[kafka-watch] registering task definition..." >&2
REV=$(aws ecs register-task-definition --cli-input-json "$TD_JSON" \
  --query 'taskDefinition.revision' --output text)
echo "[kafka-watch] task-definition rev=$REV" >&2

TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" \
  --task-definition "${PREFIX}-kafka-watch:$REV" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG}],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)

TASK_ID="${TASK_ARN##*/}"
STREAM="kafka-watch/watcher/${TASK_ID}"

echo "[kafka-watch] task=${TASK_ID}" >&2
echo "[kafka-watch] waiting ~30s for container to start..." >&2

# Espera hasta que el task arranque y tenga log stream
until aws logs describe-log-streams \
        --log-group-name "$LOG_GROUP" \
        --log-stream-name-prefix "$STREAM" \
        --query 'logStreams[0].logStreamName' --output text 2>/dev/null \
        | grep -q "kafka-watch"; do
  sleep 5
done

echo "[kafka-watch] ── live tail (Ctrl+C to exit; task auto-dies after ${DURATION}s) ──" >&2
echo "" >&2

# Tail en vivo — hasta Ctrl+C o fin del task
aws logs tail "$LOG_GROUP" \
  --follow \
  --log-stream-name-prefix "$STREAM" \
  --format short

# Una vez el user hace Ctrl+C, intentamos limpiar por prolijidad (best-effort)
echo "" >&2
echo "[kafka-watch] stopping task..." >&2
aws ecs stop-task --cluster "$CLUSTER" --task "$TASK_ARN" \
  --reason "user-requested kafka-watch stop" >/dev/null 2>&1 || true
echo "[kafka-watch] done" >&2
