#!/usr/bin/env bash
# Downloads the Solace Kafka Source Connector (Solace → Kafka/Redpanda direction)
# and extracts the JARs into ./plugins/ so the Dockerfile can COPY them in
# without needing a package manager inside the container.
#
# Run from the project root (run_demo.sh calls this automatically):
#   bash config/kafka-connect/download-connector.sh
#
# To use a different version:
#   SOLACE_CONNECTOR_VERSION=3.3.0 bash config/kafka-connect/download-connector.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="${SCRIPT_DIR}/plugins"
VERSION="${SOLACE_CONNECTOR_VERSION:-3.3.0}"
ZIP_URL="https://solaceproducts.github.io/pubsubplus-connector-kafka-source/downloads/pubsubplus-connector-kafka-source-${VERSION}.zip"
TMP_ZIP="/tmp/solace-connector-${VERSION}.zip"

log() { echo "[download-connector] $*"; }

# Skip if JARs are already present
if ls "${PLUGINS_DIR}"/*.jar &>/dev/null 2>&1; then
  log "Connector JARs already present in ${PLUGINS_DIR} — skipping download."
  log "  (Delete ${PLUGINS_DIR}/*.jar to force re-download)"
  exit 0
fi

log "Downloading Solace Kafka Source Connector v${VERSION} (Solace → Redpanda)..."
log "  URL: ${ZIP_URL}"

mkdir -p "${PLUGINS_DIR}"

curl -fsSL --progress-bar -L "${ZIP_URL}" -o "${TMP_ZIP}"

log "Extracting JARs from lib/ to ${PLUGINS_DIR}..."
# Extract JARs from the nested lib/ directory, flat into plugins/
python3 -c "
import zipfile, io, os, shutil
data = open('${TMP_ZIP}','rb').read()
dest = '${PLUGINS_DIR}'
with zipfile.ZipFile(io.BytesIO(data)) as z:
    for name in z.namelist():
        if name.endswith('.jar') and '/lib/' in name:
            basename = os.path.basename(name)
            target = os.path.join(dest, basename)
            with z.open(name) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            print(f'  extracted: {basename}')
"
rm -f "${TMP_ZIP}"

JAR_COUNT=$(ls "${PLUGINS_DIR}"/*.jar 2>/dev/null | wc -l | tr -d ' ')
log "Done. ${JAR_COUNT} JAR(s) in ${PLUGINS_DIR}/"
ls -lh "${PLUGINS_DIR}/"
