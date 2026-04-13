# Custom RisingWave image with the Solace source connector.
# The Solace connector has not been merged upstream yet — this image
# packages the pre-built binary from the fork at:
#   https://github.com/jessemenning/risingwave (branch feat/solace-source-connector)
#
# Rebuild: docker build -t ghcr.io/jessemenning/risingwave:solace-connector risingwave-solace/
FROM risingwavelabs/risingwave:latest
COPY risingwave-stripped /risingwave/bin/risingwave
