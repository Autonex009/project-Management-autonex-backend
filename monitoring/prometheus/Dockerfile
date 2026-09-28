FROM prom/prometheus:latest

COPY prometheus.yml /etc/prometheus/prometheus.yml
COPY entrypoint.sh /etc/prometheus/entrypoint.sh

# The base image runs as nobody, which cannot write to a Railway volume.
# Without this the TSDB fails to open and the service crashloops.
USER root

# Invoked through sh so the script does not need an executable bit, which git on
# Windows would not preserve anyway.
ENTRYPOINT ["/bin/sh", "/etc/prometheus/entrypoint.sh"]
# Storage lives at /data, not the image's own /prometheus: that path is declared
# as a VOLUME in the base image, and mounting a Railway volume over it hangs the
# deploy at "Creating containers". Grafana's image declares no VOLUME, which is
# why its mount works.
CMD [ \
  "--config.file=/etc/prometheus/prometheus.yml", \
  "--storage.tsdb.path=/data", \
  "--storage.tsdb.retention.time=15d", \
  "--web.listen-address=[::]:9090" \
]
