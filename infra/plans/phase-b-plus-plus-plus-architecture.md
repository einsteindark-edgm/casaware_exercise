# Phase B+++ — Por qué la arquitectura funciona, y por qué los intentos previos fallaron

> Documento de aprendizaje. La estrella no es la arquitectura final sino la cadena de descubrimientos que llevó a ella. Si entendés el "por qué no" de cada paso descartado, entendés todo el sistema.

---

## Diagrama final (la que funciona)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  AWS Account (525237381234)                                              │
│                                                                          │
│   ┌──────────────────────┐         ┌──────────────────────────────┐      │
│   │ MSK VPC (10.0/16)    │         │ Databricks workspace VPC      │     │
│   │                      │         │ (10.227/16, customer-managed)│      │
│   │ ┌──────────────────┐ │         │                              │      │
│   │ │MSK Provisioned   │ │         │ ┌──────────────────────────┐ │      │
│   │ │2 brokers IAM SASL│◄┤◄────────┤►│DLT Classic Cluster        │ │     │
│   │ │9098              │ │ peering │ │m5.large, dlt:17.3.8       │ │     │
│   │ └──────────────────┘ │ + R53   │ │UC Service Credential auth │ │     │
│   │                      │ Resolver│ └──────────────────────────┘ │      │
│   │ ┌──────────────────┐ │         │                              │      │
│   │ │Debezium ECS      │ │         │                              │      │
│   │ │(productor)       │ │         │                              │      │
│   │ └──────────────────┘ │         │                              │      │
│   └──────────────────────┘         └──────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────────────┘

MongoDB Atlas
    │ change streams
    ▼
Debezium Server (Fargate, vpc 10.0)
    │ IAM SASL 9098
    ▼
MSK Provisioned (vpc 10.0)
    │ ◄── VPC peering 10.0/16 ↔ 10.227/16
    │ ◄── Route53 Resolver (forwarding rule kafka.us-east-1.amazonaws.com)
    │ ◄── UC Service Credential firma SASL IAM
    ▼
DLT Classic compute (vpc 10.227, workspace VPC)
    │ triggered cada 10 min (cdc_refresh job)
    ▼
nexus_dev.bronze.* (Delta tables, UC managed)
    │ apply_changes SCD1 cada 10 min
    ▼
nexus_dev.silver.*
    │ aggregaciones cada 10 min
    ▼
nexus_dev.gold.*
```

---

## Conceptos base (entender estos primero)

### 1. VPC, peering, route tables y la red en AWS

Una **VPC** es una red privada dentro de AWS, identificada por un CIDR (rango de IPs). Toda VPC tiene:
- **Subnets**: subdivisiones del CIDR, asociadas a una AZ.
- **Route tables**: deciden a dónde se envía el tráfico saliente de cada subnet (next hop por destino).
- **Security groups**: stateful firewalls a nivel de ENI (interfaz de red).

Dos VPCs diferentes son redes **completamente aisladas** por defecto. Aunque vivan en el mismo AWS account, no se ven una a la otra. Para conectarlas:

- **VPC peering**: una conexión 1:1 entre dos VPCs (mismo o distinto account). No es transitiva (A↔B y B↔C no implica A↔C).
- Después del peering hay que **agregar rutas** en ambos lados (`destinationCIDR del otro VPC → peering connection ID`).
- Los CIDRs no pueden solaparse.
- Las security groups del lado peer **no se pueden referenciar por ID** en otra VPC (sólo same-VPC SGs); hay que usar CIDRs.

En nuestro caso: MSK vive en `vpc-016b9359` (10.0/16). El workspace de Databricks vive en `vpc-08a19874` (10.227/16). Sin peering, los dos no se ven nada — aunque sean del mismo account.

### 2. DNS en VPC y Route53 Resolver

AWS provee un servidor DNS por VPC (la "AmazonProvidedDNS", IP `VPC.2`, p.ej. `10.0.0.2`). Resuelve:
1. Nombres públicos de internet (`google.com`).
2. Nombres internos de la VPC (`ec2-XX.compute.internal`).
3. **Private hosted zones de Route53** asociadas a la VPC.

Ahí está el detalle crítico: las **private hosted zones** son mapeos hostname→IP que viven en una zona Route53 específica. Una zona puede asociarse con N VPCs vía `aws_route53_zone_association`. Si X.com está en una zona privada asociada a VPC-A pero NO a VPC-B, una query DNS desde VPC-B para X.com falla aunque las dos VPCs estén peered.

Tres patrones de resolución cross-VPC:
- **A) Asociar la misma zona privada a múltiples VPCs** (`aws_route53_zone_association`). Funciona si vos sos el owner de la zona.
- **B) Route53 Resolver Rules**: definís que queries para `dominio.X` se reenvían (vía un endpoint outbound) a una IP target (un endpoint inbound en otra VPC).
- **C) Custom domain con CNAMEs** (workaround): crear tu propia zona y poner CNAMEs hacia el FQDN real. Rompe TLS hostname verification.

### 3. AWS PrivateLink y VPC Endpoint Services

PrivateLink es un mecanismo para exponer un servicio TCP en una VPC consumido como un endpoint privado en otra VPC, sin peering ni internet pública. Componentes:

- **VPC Endpoint Service** (lado del provider): le pone una "fachada" a tu NLB para que terceros lo consuman.
- **VPC Endpoint** (lado del consumer): una ENI en su VPC que apunta al endpoint service del provider. Aparece como una IP privada local.
- **Allowed principals**: lista de AWS accounts/roles que pueden crear endpoints contra tu service.
- TCP-only (NLB pasa-a-través). Auth a nivel de aplicación (TLS, IAM SASL, etc.) sigue siendo responsabilidad del provider.

PrivateLink sirve para conectar VPCs sin peering — útil cuando los CIDRs solapan, o cuando el consumer está en otro AWS account o en una red administrada por un tercero (como Databricks Serverless compute).

### 4. MSK (Managed Streaming for Kafka): Serverless vs Provisioned

**MSK Serverless**:
- Sin brokers explícitos. AWS gestiona capacidad.
- Solo soporta auth IAM SASL (puerto 9098).
- Los brokers tienen DNS público pero IPs privadas — **solo accesible desde la VPC owner o vía PrivateLink**.
- No expone una "private hosted zone" que se pueda asociar a otras VPCs.
- No soporta `advertised.listeners` custom.

**MSK Provisioned**:
- Brokers explícitos (instance type, número, storage). Pagás por hora corra o no.
- Soporta IAM SASL, SCRAM, mTLS.
- Los brokers tienen FQDN tipo `b-N.<cluster>.<uuid>.c2.kafka.<region>.amazonaws.com:9098`.
- El zone privada que mapea esos FQDN a IPs privadas está **administrada por AWS**, **no se puede compartir con otras VPCs** vía `route53_zone_association` (es invisible al cliente).
- Soporta más patrones de exposición (NLB+VPCES per broker, custom DNS, etc.).

### 5. Databricks compute: Serverless vs Classic vs DLT

Databricks tiene varios "tipos de compute" que viven en lugares distintos de la red:

| Compute | Vive en |
|---|---|
| **Classic clusters / classic DLT / classic warehouses** | VPC del workspace (Databricks-managed o **customer-managed**) |
| **Serverless SQL warehouses** | VPC administrada por Databricks, fuera de tu account |
| **Serverless Jobs / Notebook compute** | VPC administrada por Databricks |
| **Serverless DLT (Lakeflow SDP)** | VPC administrada por Databricks, **distinta** de las anteriores |

**Customer-managed VPC**: en lugar de que Databricks cree el workspace en su VPC, vos le decís "usá esta VPC mía con estas subnets/SGs". Permite peering directo desde tus otras VPCs hacia la del workspace, IAM nativo, etc. Es una decisión de creación del workspace — no se puede cambiar después.

### 6. Databricks NCC (Network Connectivity Configuration) y PrivateLink

NCC es el mecanismo que tiene Databricks para que sus servicios **serverless** se conecten a recursos privados de tu AWS:

- Vos creás un **VPC Endpoint Service** en tu VPC (con un NLB delante de tu recurso, p.ej. MSK).
- Creás un **NCC** en tu Databricks account (region-scoped).
- Creás **private endpoint rules** dentro del NCC apuntando al VPCES.
- Adjuntás el NCC al workspace via `account workspaces update`.
- Databricks crea entonces un VPC Endpoint en su VPC serverless conectado a tu VPCES.
- Registrás `domain_names` en la rule para que el DNS interno de Databricks sepa que `mi-recurso.example.com` debe resolver vía ese endpoint.

**Limitaciones críticas**:
- NCC requiere **tier ENTERPRISE** o superior.
- NCC **no soporta DLT Serverless por default** (gated por Databricks support). Sí soporta SQL warehouses, Jobs serverless, Model Serving.
- NCC es **region-scoped**: el NCC us-east-1 solo conecta servicios serverless en us-east-1. Un workspace us-west-2 NO PUEDE consumir un NCC us-east-1.
- DNS chasing (resolver un FQDN cualquiera): NO. Sólo dominios explícitamente registrados.
- VPCES `allowed_principals` debe incluir el AWS account del control-plane Serverless de Databricks (rotativo).

### 7. Unity Catalog Service Credentials

Una **Service Credential** es un objeto en Unity Catalog que registra un IAM Role para que los notebooks/jobs lo asuman para autenticarse contra servicios externos sin usar instance profile.

- El role tiene una trust policy que acepta al UC Master Role de Databricks (`arn:aws:iam::414351767826:role/unity-catalog-prod-UCMasterRole-...`) con `sts:ExternalId == <UUID generado por el metastore>`.
- Cuando un notebook usa `.option("databricks.serviceCredential", "<name>")`, Databricks asume el role y firma la request al servicio externo (ej. SASL IAM con MSK).
- **No requiere que el cluster tenga instance profile**. Eso significa que clusters classic con UC pueden hacer auth contra MSK sin necesidad de un IAM role attachable al EC2.
- Soportado en DBR ≥16.1 y en DLT (classic + serverless) y en SQL warehouses.

---

## La cadena de descubrimientos: qué falló y por qué

La meta era: **MongoDB → bronze table en Databricks**, sin un timer ETL artificial. Llevamos 4 intentos antes del que funcionó. Cada uno reveló una pieza nueva de la red.

### Intento 1 — MSK Serverless + DLT Serverless directo

**Idea**: Debezium publica a topics MSK Serverless. DLT Serverless lee esos topics con `spark.readStream.format("kafka")`. Sin peering, sin nada — porque MSK Serverless tiene "DNS público con IAM auth", supuestamente accesible desde cualquier VPC con credenciales válidas.

**Por qué falló**:
- Error: `ConfigException: No resolvable bootstrap urls given in bootstrap.servers`.
- Las "DNS públicas" de MSK Serverless resuelven a IPs **privadas** de la VPC donde lo creaste. Desde otra VPC (la de Databricks Serverless) la query DNS fracasa aunque la zona sea pública porque las IPs no son ruteables.
- MSK Serverless no expone la zona privada a otros consumers.

**Lección**: la "auth IAM-gated" de MSK Serverless (puerto 9098 desde 0.0.0.0/0) es un canard de seguridad — **el problema es ruteo, no auth**. Y sin peering ni PrivateLink no hay conexión.

### Intento 2 — Workspace en us-west-2 + MSK Provisioned + NCC

**Idea**: Migrar a MSK Provisioned + NCC + PrivateLink. NCC sí soporta Provisioned con el patrón "NLB per broker", garantizando conectividad sin importar la VPC del consumer.

**Por qué falló**:
- El workspace de Databricks usado para el bundle deploy estaba en **us-west-2**. MSK + el resto de AWS estaban en **us-east-1**.
- NCC es region-scoped. Un NCC us-east-1 no puede ser referenciado desde un workspace us-west-2 ni viceversa.
- Aunque el workspace fuera us-east-1, también descubrimos:
- DLT Serverless **no consume NCC private endpoint rules por default**. Hay un gate del lado Databricks que requiere abrir un caso de soporte para habilitar PrivateLink en DLT Serverless. Otros productos serverless (SQL warehouses, Jobs) sí lo consumen automáticamente.
- Aunque el NCC tuviera estado `ESTABLISHED` y los VPC endpoints conectaran, las queries DNS desde compute Serverless DLT seguían fallando porque la integración de NCC con DLT Serverless no está activa.

**Confirmación oficial**: docs.databricks.com/aws/en/ldp/serverless dice textualmente:
> *"If you need to use an AWS PrivateLink connection with your serverless Lakeflow Spark Declarative Pipelines, contact your Databricks representative."*

**Lección**:
- **Verificar región del workspace antes que cualquier cosa de red**. Migrar workspace fue un costo recuperable; haberlo descubierto al final habría sido peor.
- **Cada producto Databricks tiene su propia integración con NCC**. No asumir que "serverless = NCC". Leer la lista oficial de productos soportados.

### Intento 3 — DLT Classic + VPC peering (sin Route53 Resolver)

**Idea**: Si DLT Classic corre en la VPC del workspace (customer-managed, 10.227/16), basta con peer-ear con la VPC de MSK (10.0/16). El compute es entonces "tu propia VPC" y el DNS de MSK debería resolverse vía la AmazonProvidedDNS standard.

**Por qué falló**:
- DNS again. La zona privada `*.kafka.us-east-1.amazonaws.com` de MSK es **AWS-managed**. Aunque haya peering + route tables + `allow_remote_vpc_dns_resolution=true`, la zona no se puede asociar a la VPC peered. AWS la mantiene como una zona interna invisible al usuario.
- Resultado: los FQDNs `b-1.<cluster>...kafka.us-east-1.amazonaws.com` siguen siendo NXDOMAIN desde la VPC del workspace.

**Lección**: peering + DNS resolution flag !== private hosted zone association. Son cosas distintas. AWS expone la asociación de zona como un concepto separado, y para zonas AWS-managed no hay forma de asociarlas externamente.

### Intento 4 — DLT Classic + VPC peering + Route53 Resolver

**Idea**: Si no podemos asociar la zona privada de MSK a la VPC del workspace, la pasamos por proxy. Un Route53 Resolver Rule dice: "queries para `kafka.us-east-1.amazonaws.com` desde la VPC del workspace, reenviálas a estas IPs (un Resolver inbound endpoint en la VPC de MSK)". El inbound endpoint está adentro de MSK VPC, donde la zona privada es resoluble.

**Componentes**:

```
[Databricks workspace VPC 10.227/16]
   │
   │  DLT Classic cluster necesita resolver "b-1.nexusdevedgm....kafka.us-east-1.amazonaws.com"
   │
   ▼
AmazonProvidedDNS @ VPC 10.227 (10.227.0.2)
   │
   │  consulta Route53 Resolver Rules asociadas a la VPC
   │
   ▼
Resolver Rule: "kafka.us-east-1.amazonaws.com → forward a estas IPs"
   │
   ▼
Resolver outbound endpoint @ VPC 10.227 (2 ENIs en subnets distintas)
   │
   │  DNS UDP 53 sobre peering hacia
   │
   ▼
Resolver inbound endpoint @ VPC 10.0 (10.0.11.99, 10.0.12.109)
   │
   │  resuelve usando la zona privada AWS-managed de MSK (visible solo en VPC 10.0)
   │
   ▼
respuesta: "b-1.... → 10.0.11.68"
   ▲
   │  el DNS reply viaja de regreso por el mismo path
   │
DLT Classic cluster recibe IP 10.0.11.68
   │
   │  abre TCP a 10.0.11.68:9098 sobre el peering
   │
   ▼
Broker MSK acepta la conexión (SG permite 9098 desde 10.227/16)
```

**Después también falló auth (sub-intento 4a)**:
- Conexión TCP exitosa. Pero al iniciar SASL IAM:
- Error: `Failed to find AWS IAM Credentials [Caused by ... Unable to load AWS credentials from any provider in the chain]`.
- El cluster classic no tenía instance profile attached, y los notebooks usaban `kafka.sasl.jaas.config = "shadedmskiam.IAMLoginModule required"`. Ese login module busca AWS credentials por la cadena default del SDK (env vars, instance profile, etc.) → no encuentra nada.

### Intento 5 — UC Service Credential auth

**Cambio**: en los notebooks, reemplazar inline JAAS por `databricks.serviceCredential`. Databricks DBR ≥16.1 con UC Service Credential firma la SASL request al MSK con el role registrado en UC, sin necesidad de instance profile.

**Detalle adicional descubierto**: cuando se pasa `.option("databricks.serviceCredential", ...)`, **NO se pueden** pasar también `.option("kafka.security.protocol", "SASL_SSL")` ni `.option("kafka.sasl.mechanism", "AWS_MSK_IAM")`. Databricks los inyecta automáticamente y genera error si los pasás manualmente:
> *"SASL parameters kafka.sasl.mechanism, kafka.security.protocol are not allowed in source options when using a service credential."*

Resultado: bronze pipeline `COMPLETED`, las 5 streaming tables creadas, eventos del topic ingresan a las tablas Delta.

---

## Por qué la combinación final SÍ funciona

Cada decisión arquitectónica resuelve un problema específico:

| Decisión | Resuelve |
|---|---|
| **Workspace customer-managed VPC** | Permite peer directo desde otra VPC sin necesitar PrivateLink. |
| **MSK Provisioned** (vs Serverless) | Brokers tienen IPs estables y FQDNs accesibles vía Route53 Resolver. Permite NLB-per-broker pattern (que terminamos no usando, pero da flexibilidad). |
| **DLT Classic** (vs Serverless) | El compute corre en la VPC del workspace, donde tenemos control total de red. Evita el gate de NCC para Serverless DLT. |
| **VPC peering 10.227/16 ↔ 10.0/16** | Establece ruta IP bidireccional entre las dos VPCs. Sin esto, el TCP nunca llega. |
| **`allow_remote_vpc_dns_resolution=true`** ambos lados del peering | Habilita que las queries DNS atraviesen el peer. No suficiente por sí solo, pero requerido. |
| **Route53 Resolver inbound + outbound + forwarding rule** | Salva el problema de la zona privada AWS-managed de MSK. Sin esto, el DNS de brokers es NXDOMAIN desde la VPC del workspace. |
| **MSK SG ingress 9098 desde 10.227/16** | Sin esto el TCP llega al broker pero el SG lo bloquea. |
| **UC Service Credential** | Permite que el cluster classic firme SASL IAM sin instance profile. Mantiene auth gobernada por UC (mejor que IAM nativo del cluster). |
| **DBR ≥16.1** (`spark_version: "16.4.x-scala2.12"`, ignorado por DLT que usa su runtime) | UC Service Credential como `databricks.serviceCredential` requiere DBR moderno. DLT 17.3 cumple. |
| **Pipeline triggered + schedule cada 10 min** | Cluster classic continuous costaría ~$228/mo. Triggered con startup ~3 min × 144 runs/día ≈ ~$25/mo. Latencia aceptable para dev. |

### Flujo de un evento en condiciones normales

1. Usuario crea un expense desde el frontend.
2. Backend escribe en MongoDB Atlas (`nexus_dev.expenses` collection).
3. Mongo emite un change stream event.
4. Debezium Server (ECS Fargate, vpc 10.0) consume el change stream y publica al topic `nexus.nexus_dev.expenses` en MSK Provisioned (vpc 10.0). Auth via IAM SASL (instance role del task ECS).
5. Aproximadamente cada 10 min, Databricks Jobs scheduler dispara `nexus_cdc_refresh`:
   - Task `bronze_cdc`: arranca un cluster DLT Classic (m5.large, 1-2 workers, en una subnet de vpc 10.227).
   - El cluster necesita resolver `b-1.nexusdevedgmkafkaprov.ygf4z6.c23.kafka.us-east-1.amazonaws.com`:
     - Query a la AmazonProvidedDNS de vpc 10.227.
     - El resolver detecta que el dominio matchea la rule `kafka.us-east-1.amazonaws.com`.
     - Reenvía via outbound resolver endpoint a las IPs del inbound resolver (10.0.11.99 / 10.0.12.109).
     - El packet UDP 53 viaja por el peering.
     - El inbound resolver, dentro de vpc 10.0, consulta la zona privada AWS-managed de MSK y obtiene la IP del broker (10.0.11.68).
     - La respuesta viaja de regreso por el mismo path.
   - El cluster abre TCP a 10.0.11.68:9098 sobre el peering. SG del broker permite ingress desde 10.227/16.
   - SASL IAM handshake: el cluster usa UC Service Credential para asumir el role `nexus-dev-edgm-msk-databricks` y firmar la request. Role tiene `kafka-cluster:Connect/Read/...` sobre el ARN del cluster.
   - Bronze notebooks consumen los topics, hacen `from_json` sobre el value, escriben a Delta tables `nexus_dev.bronze.*`.
   - Task `silver`: cuando bronze termina, otro pipeline DLT Classic (también triggered) hace `apply_changes` SCD1 sobre las tablas bronze.
   - Task `gold`: tras silver, un tercer pipeline genera las tablas de agregación.
6. Latencia total Mongo → bronze: ≤ 13 min worst case (10 min trigger + 3 min cluster startup + segundos de ingestión).

---

## Conceptos para llevarse

1. **Network y DNS son layers separadas**. Peering resuelve ruteo IP. Para que el DNS resuelva a través del peer, hay un patrón distinto (zone association o Resolver Rules).

2. **Las zonas privadas de servicios AWS-managed (MSK, RDS, ElastiCache, etc.) no son visibles a otros consumers**. Si necesitás resolverlas cross-VPC, **Route53 Resolver Rules** es el patrón soportado.

3. **No todo "Serverless de Databricks" es igual**. Cada producto tiene su propia matriz de soporte para NCC, customer-managed VPC, IP allow lists. Leer las docs específicas del producto.

4. **NCC vs VPC peering** son alternativas, no complementarias:
   - NCC: para conectar Databricks Serverless con tus recursos privados, sin que tengas control sobre la red de Databricks.
   - VPC peering: para conectar tu workspace customer-managed con otras VPCs propias.
   - El customer-managed VPC permite usar peering nativo sin necesidad de NCC.

5. **UC Service Credentials > instance profiles** para auth de clusters contra servicios AWS:
   - Auth gobernada por UC (auditable, revocable).
   - No requiere modificar los nodos del cluster.
   - Funciona en DLT classic + serverless, SQL warehouses, jobs (todos con DBR ≥16.1).
   - Pero hay restricciones: con `databricks.serviceCredential` no se pueden mezclar opciones SASL manualmente.

6. **Triggered DLT pipelines** son una forma legítima de bajar costo en dev. El cluster vive solo durante el run (~ 3 min startup + tiempo de proceso). Usar `cdc_refresh` job con `schedule:` y dependencies entre tasks (bronze → silver → gold) en vez de pipelines continuous.

7. **Iterar con confianza requiere observabilidad**:
   - DLT pipeline events (`databricks pipelines list-pipeline-events ... --filter "level='ERROR'"`) revelan la cadena de excepciones.
   - Cada exception class apunta a un layer distinto: `ConfigException` → DNS, `SaslAuthenticationException` → auth, `KafkaIllegalStateException` → option mismatch.

8. **Verificar la región y la VPC del workspace antes que cualquier otra cosa**. Eso ahorra días.

---

## Recursos terraform clave

| Archivo | Qué hace |
|---|---|
| `infra/terraform/msk.tf` | MSK Provisioned cluster + SG + cluster_policy. |
| `infra/terraform/vpc_peering.tf` | VPC peering, routes, MSK SG ingress 10.227/16, Route53 Resolver inbound/outbound + forwarding rule + association. |
| `infra/terraform/databricks.tf` | Storage credential UC, external location, catalog, schemas. |
| `infra/terraform/databricks_msk.tf` | IAM role para UC Service Credential que firma SASL IAM contra MSK. |
| `infra/terraform/debezium.tf` | ECS task de Debezium, IAM perms para producir a MSK, env vars del connector. |
| `nexus-medallion/resources/pipelines/bronze_cdc.yml` | DLT Classic + clusters config + serviceCredential. |
| `nexus-medallion/resources/jobs/cdc_refresh.yml` | Job que orquesta bronze → silver → gold cada 10 min. |
| `nexus-medallion/src/bronze/cdc_*.py` | 5 notebooks DLT que leen Kafka topics → Delta tables, usando `databricks.serviceCredential`. |

---

## Para profundizar más

- Route53 Resolver: https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-rules-managing.html
- VPC peering DNS: https://docs.aws.amazon.com/vpc/latest/peering/modify-peering-connections.html#vpc-peering-dns
- MSK auth IAM: https://docs.aws.amazon.com/msk/latest/developerguide/iam-access-control.html
- Databricks NCC: https://docs.databricks.com/aws/en/security/network/serverless-network-security/pl-to-internal-network
- Databricks UC Service Credentials: https://docs.databricks.com/aws/en/connect/unity-catalog/cloud-services/service-credentials
- DLT Classic compute config: https://docs.databricks.com/aws/en/dlt/configure-compute
- Customer-managed VPC: https://docs.databricks.com/aws/en/security/network/classic/customer-managed-vpc
