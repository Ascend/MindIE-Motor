SHELL := /bin/bash

TAG ?= master
REGISTRY ?= docker.io
IMAGE_NAME ?= mindie-motor-vllm
IMAGE ?= $(if $(strip $(REGISTRY)),$(REGISTRY)/,)$(IMAGE_NAME):$(TAG)

DOCKERFILE ?= docker/mindie-motor-vllm/master/Dockerfile
BASE_IMAGE ?= quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0
IMAGE_VERSION ?= master
PIP_INDEX_URL ?= https://repo.huaweicloud.com/repository/pypi/simple
PIP_TRUSTED_HOST ?= repo.huaweicloud.com
NETWORK ?= host

# Cloud Native Deploy image
DEPLOYER_TAG ?= latest
DEPLOYER_IMAGE_NAME ?= mindie-pymotor-deployer
DEPLOYER_IMAGE ?= $(REGISTRY)/$(DEPLOYER_IMAGE_NAME):$(DEPLOYER_TAG)
DEPLOYER_DOCKERFILE := examples/cloud_native_deploy/Dockerfile

PLATFORMS ?= linux/arm64
OUTPUT ?= type=docker

DOCKER_BUILD_ARGS = \
	--network=$(NETWORK) \
	--build-arg BASE_IMAGE="$(BASE_IMAGE)" \
	--build-arg IMAGE_VERSION="$(IMAGE_VERSION)" \
	--build-arg PIP_INDEX_URL="$(PIP_INDEX_URL)" \
	--build-arg PIP_TRUSTED_HOST="$(PIP_TRUSTED_HOST)"

.PHONY: help build-wheel build-pymotor-image build-deployer-image

help:
	@echo "Available targets:"
	@echo "  make build-wheel"
	@echo "  make build-deployer-image [DEPLOYER_TAG=latest REGISTRY=... PLATFORMS=linux/amd64,linux/arm64]"
	@echo "  make build-pymotor-image"
	@echo
	@echo "Examples:"
	@echo "  # Build vLLM-Ascend v0.18.0 based A2 image and load it locally"
	@echo "  make build-pymotor-image BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0 PLATFORMS=linux/arm64"
	@echo
	@echo "  # Build vLLM-Ascend v0.18.0 based A3 image and load it locally"
	@echo "  make build-pymotor-image BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3 PLATFORMS=linux/arm64 TAG=master-a3"
	@echo
	@echo "  # Build and push a multi-platform image"
	@echo "  make build-pymotor-image REGISTRY=example.com/team PLATFORMS=linux/amd64,linux/arm64 OUTPUT=type=registry"

build-wheel:
	./build.sh

# Build the current working tree. OUTPUT=type=docker loads a single-platform image locally;
# OUTPUT=type=registry supports pushing multi-platform images.
build-pymotor-image:
	@if [[ "$(OUTPUT)" == type=registry* && -z "$(REGISTRY)" ]]; then \
		echo "REGISTRY is required when OUTPUT=$(OUTPUT)" >&2; \
		exit 1; \
	elif [[ "$(OUTPUT)" == type=docker* && "$(PLATFORMS)" == *,* ]]; then \
		echo "OUTPUT=$(OUTPUT) only supports one platform; use OUTPUT=type=registry for multiple platforms" >&2; \
		exit 1; \
	fi
	docker buildx build $(DOCKER_BUILD_ARGS) \
		--platform "$(PLATFORMS)" \
		--output "$(OUTPUT)" \
		-t "$(IMAGE)" \
		-f "$(DOCKERFILE)" \
		.

# Build the deployer image
build-deployer-image:
	docker buildx build -t "$(DEPLOYER_IMAGE)" -f "$(DEPLOYER_DOCKERFILE)" . --output "$(OUTPUT)" --platform $(PLATFORMS)
