FROM grafana/grafana:latest

# Datasource and dashboard provisioning, so a fresh volume comes up already
# wired instead of needing click-through setup after every rebuild.
COPY provisioning /etc/grafana/provisioning

# Not /var/lib/grafana: a Railway volume mounts there and would shadow these.
COPY dashboards /etc/grafana/dashboards

# The base image runs as uid 472, which cannot write to a Railway volume
# mounted at /var/lib/grafana.
USER root
