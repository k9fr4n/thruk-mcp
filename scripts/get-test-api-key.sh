#!/usr/bin/env bash
# Generate a Thruk API key inside the OMD demo container.
#
# Prereqs: `docker compose -f compose.test.yml up -d` is running and the
# OMD site `demo` has finished bootstrapping (~30s after `up`).
#
# The key is created against the `omdadmin` user (default OMD demo admin)
# with full system privileges. Echoes the raw key on stdout.
set -euo pipefail
CONTAINER="${CONTAINER:-thruk-mcp-omd}"
SITE="${OMD_SITE:-demo}"
USER="${THRUK_USER:-omdadmin}"

docker exec -i "$CONTAINER" su - "$SITE" -c \
  "thruk r -m POST '/thruk/api_keys' \
      -d 'comment=integration-test' -d 'username=$USER' -d 'superuser=1'" \
  | jq -r '.private_key'
