FROM confluentinc/cp-kafka-connect:7.6.0

# The cp-kafka-connect 7.x image has no package manager.
# Connector JARs are pre-downloaded to ./plugins/ on the host by
# config/kafka-connect/download-connector.sh before this image is built.
# See run_demo.sh — it calls that script automatically.

USER root
RUN mkdir -p /usr/share/java/solace-connector
COPY plugins/ /usr/share/java/solace-connector/
RUN chown -R appuser:appuser /usr/share/java/solace-connector
USER appuser
