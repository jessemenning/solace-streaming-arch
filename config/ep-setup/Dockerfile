FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl jq postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p config/risingwave

# Build context is project root — copy scripts from their source locations
COPY create_ep_objects.sh generate_mvs.py config/ep-setup/entrypoint.sh ./
RUN chmod +x create_ep_objects.sh entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
