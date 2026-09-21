FROM prom/prometheus:latest

COPY prometheus.yml /etc/prometheus/prometheus.yml
COPY entrypoint.sh /etc/prometheus/entrypoint.sh

# The base image runs as nobody, which cannot write to a Railway volume mounted
# at /prometheus. Without this the TSDB fails to open and the service crashloops.
USER root

# Invoked through sh so the script does not need an executable bit, which git on
# Windows would not preserve anyway.
ENTRYPOINT ["/bin/sh", "/etc/prometheus/entrypoint.sh"]
CMD [ \
  "--config.file=/etc/prometheus/prometheus.yml", \
  "--storage.tsdb.path=/prometheus", \
  "--storage.tsdb.retention.time=15d", \
  "--web.listen-address=[::]:9090" \
]
