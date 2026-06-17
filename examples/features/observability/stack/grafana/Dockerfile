ARG GRAFANA_VERSION=11.3.0
FROM grafana/grafana:${GRAFANA_VERSION}

LABEL org.opencontainers.image.title="pymotor-grafana" \
      org.opencontainers.image.description="Grafana with pre-provisioned datasources & dashboards for pymotor observability stack."

ENV GF_SECURITY_ADMIN_USER=motor \
    GF_SECURITY_ADMIN_PASSWORD=motor \
    GF_USERS_ALLOW_SIGN_UP=false \
    GF_INSTALL_PLUGINS="" \
    GF_LOG_LEVEL=warn

COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/   /var/lib/grafana/dashboards/

EXPOSE 3000
