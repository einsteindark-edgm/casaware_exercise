# Debezium Server 3.0.0.Final + MSK IAM auth.
#
# Debezium 3.0 ships AWS SDK v2 at 2.13.13, too old for aws-msk-iam-auth
# 2.3.5 which needs methods (asyncCredentialUpdateEnabled, CollectionUtils
# .uniqueIndex) introduced in later SDK versions. Upgrade the whole SDK v2
# family to 2.25.70 in a single layer, then add the MSK IAM uber jar.
#
# The classpath loaded by /debezium/run.sh is LIB_PATH="lib/*", so we
# drop everything into /debezium/lib/.
FROM debezium/server:3.0.0.Final

USER root

# Remove stale SDK 2.13.13 jars.
RUN rm -f /debezium/lib/annotations-2.13.13.jar \
          /debezium/lib/apache-client-2.13.13.jar \
          /debezium/lib/auth-2.13.13.jar \
          /debezium/lib/aws-cbor-protocol-2.13.13.jar \
          /debezium/lib/aws-core-2.13.13.jar \
          /debezium/lib/aws-json-protocol-2.13.13.jar \
          /debezium/lib/aws-query-protocol-2.13.13.jar \
          /debezium/lib/http-client-spi-2.13.13.jar \
          /debezium/lib/netty-nio-client-2.13.13.jar \
          /debezium/lib/profiles-2.13.13.jar \
          /debezium/lib/protocol-core-2.13.13.jar \
          /debezium/lib/regions-2.13.13.jar \
          /debezium/lib/sdk-core-2.13.13.jar \
          /debezium/lib/utils-2.13.13.jar

COPY annotations-2.25.70.jar         /debezium/lib/
COPY apache-client-2.25.70.jar       /debezium/lib/
COPY auth-2.25.70.jar                /debezium/lib/
COPY aws-cbor-protocol-2.25.70.jar   /debezium/lib/
COPY aws-core-2.25.70.jar            /debezium/lib/
COPY aws-json-protocol-2.25.70.jar   /debezium/lib/
COPY aws-query-protocol-2.25.70.jar  /debezium/lib/
COPY http-client-spi-2.25.70.jar     /debezium/lib/
COPY netty-nio-client-2.25.70.jar    /debezium/lib/
COPY profiles-2.25.70.jar            /debezium/lib/
COPY protocol-core-2.25.70.jar       /debezium/lib/
COPY regions-2.25.70.jar             /debezium/lib/
COPY sdk-core-2.25.70.jar            /debezium/lib/
COPY utils-2.25.70.jar               /debezium/lib/
COPY aws-msk-iam-auth.jar            /debezium/lib/
