FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG KUBECTL_VERSION=v1.29.6
ARG TARGETARCH
RUN if [ "${TARGETARCH}" != "amd64" ] && [ "${TARGETARCH}" != "arm64" ]; then \
      echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; \
      exit 1; \
    fi \
    && curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

WORKDIR /app

COPY examples/deployer/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

RUN chmod +x /app/examples/cloud_native_deploy/entrypoint.sh \
    /app/examples/cloud_native_deploy/entrypoint-cleanup.sh \
    /app/examples/cloud_native_deploy/entrypoint-scale.sh \
    /app/examples/cloud_native_deploy/cleanup.sh

ENTRYPOINT ["/app/examples/cloud_native_deploy/entrypoint.sh"]
