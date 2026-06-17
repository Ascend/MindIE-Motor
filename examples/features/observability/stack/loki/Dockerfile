# Minimal Loki image: static binary on scratch (no base image pull required).
# Built by scripts/build-loki-image.sh when Docker Hub is unavailable.

FROM scratch

COPY loki /loki

EXPOSE 3100 9096

ENTRYPOINT ["/loki"]
