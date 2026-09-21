#!/bin/sh
# Prometheus does not expand env vars in its config, so the scrape token is
# materialised here from METRICS_TOKEN rather than being committed to
# prometheus.yml. /tmp because the config dir is not writable by this image.
set -e

if [ -z "$METRICS_TOKEN" ]; then
  echo "WARNING: METRICS_TOKEN is empty — scrapes will 404 if the targets require it" >&2
fi

printf '%s' "$METRICS_TOKEN" > /tmp/metrics_token
chmod 600 /tmp/metrics_token

exec /bin/prometheus "$@"
