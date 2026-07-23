TAG ?= latest
PLATFORMS ?= linux/amd64,linux/arm64
REGISTRY ?= docker.io
IMAGE_NAME ?= mindie-pymotor-deployer
IMAGE ?= $(REGISTRY)/$(IMAGE_NAME):$(TAG)

DOCKERFILE_CLOUD_NATIVE_DEPLOY := examples/cloud_native_deploy/Dockerfile

.PHONY: help build-wheel buildx-cloud-native-deployer

help:
	@echo "Available targets:"
	@echo "  make build-wheel"
	@echo "  make buildx-cloud-native-deployer [TAG=latest REGISTRY=... PLATFORMS=linux/amd64,linux/arm64]"

build-wheel:
	./build.sh

buildx-cloud-native-deployer:
	docker buildx build -t "$(IMAGE)" -f "$(DOCKERFILE_CLOUD_NATIVE_DEPLOY)" . --output=type=registry --platform $(PLATFORMS)
