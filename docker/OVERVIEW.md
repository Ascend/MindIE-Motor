# MindIE-Motor

> English | [中文](./OVERVIEW.zh.md)

## Quick Reference

- MindIE-Motor is maintained by the [MindIE community](https://www.hiascend.com/cn/developer/software/mindie)

- Where to get help

    - [MindIE Image Registry](https://www.hiascend.com/developer/ascendhub/detail/af85b724a7e5469ebd7ea13c3439d48f)
    - [MindIE-Motor Documentation](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/docs/zh/index.md)
    - [Atlas Developer Community](https://www.hiascend.com/developer)
    - [Report an Issue](https://gitcode.com/Ascend/MindIE-PyMotor/issues)

---

## MindIE-Motor

Provides one-click PD-separated deployment, flexibly adapts to multiple inference engines (vLLM, SGLang) through a cloud-native plug-in architecture, and combines high-performance scheduling with load balancing capabilities to build highly available, scalable large-scale inference services.

---

## Supported Tags and Dockerfile Links

### Tag Specification

Official pre-built image tags follow this format:

```text
<MindIEVersion>-<ProductSeries>-<PythonVersion>-<OperatingSystem>-lts-<Architecture>
```

| Field | Example Value | Description |
|---|---|---|
| `MindIEVersion` | `3.0.0`、 `3.0.0b1`、 `3.0.0b2` | MindIE-Motor version number |
| `ProductSeries` | `800I-A2`, `800I-A3` | Target Atlas product series |
| `PythonVersion` | `py3.11` | Python version |
| `OperatingSystem` | `Ubuntu24.04-lts`, `openEuler24.03-lts` | Base OS and distribution identifier |
| `Architecture` | `aarch64`, `x86_64` | CPU architecture |

### 3.0.0 Dockerfile Directories

Each version/hardware/OS/architecture combination has a self-contained Dockerfile

```text
docker/mindie-motor-vllm/<tag>/Dockerfile
```

| Tag | Dockerfile |
|---|---|
| `3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-x86_64/Dockerfile) |
| `3.0.0-800I-A2-py3.11-openEuler24.03-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-openEuler24.03-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A2-py3.11-openEuler24.03-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-openEuler24.03-lts-x86_64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-x86_64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-openEuler24.03-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-openEuler24.03-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-openEuler24.03-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-openEuler24.03-lts-x86_64/Dockerfile) |

Each Dockerfile embeds the matching vllm-ascend base image (v0.18.0 series), target platform, output image tag, entrypoint script, and license agreement. No helper script, external docker files, or environment-variable selection is required.

---

## Quick Start

### Prerequisites (Optional)

#### Install Drivers

- Firmware and drivers have been installed on the host. Refer to [Install Drivers and Firmware](https://www.hiascend.com/document/detail/zh/mindie/100/envdeployment/instg/mindie_instg_0006.html) for details.
- Docker is installed on the host.

---

### Build MindIE-Motor Image

Each Dockerfile clones the pinned branch and commit during the build. **No local source tree or build context is required.** Replace `<tag>` with the desired combination:

```bash
TAG="3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64"

docker build --network=host \
    --platform=linux/arm64 \
    -t "mindie-motor-vllm:${TAG}" \
    -f "docker/mindie-motor-vllm/${TAG}/Dockerfile" \
    .
```

Each Dockerfile header comment contains the exact `--platform` value, source repository info, and full `docker build` command.

3.0.0 source mapping:

| Image Version | Repository | Branch | Commit |
|---|---|---|---|
| `3.0.0` | `https://gitcode.com/Ascend/MindIE-PyMotor.git` | `v3.0.0` | `383d1787ed3fc27aaad2db9cc5506d40c258c279` |

The build process:

1. Pulls the matching vllm-ascend base image.
2. Clones the pinned MindIE Motor source into `/opt/MindIE-PyMotor`.
3. Installs dependencies, compiles, and installs the `motor` wheel.
4. Builds and installs the `ccae_reporter` observability component.
5. Generates the entrypoint script and license agreement inline within the Dockerfile.

### Run MindIE-Motor Container

Confirm that Atlas drivers are installed on the host and `/dev/davinci*` device nodes are available.

#### Minimal Verification

```bash
IMAGE_NAME="mindie-motor-vllm:3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64"

docker run --rm -it \
  --device=/dev/davinci_manager \
  --device=/dev/devmm_svm \
  --device=/dev/hisi_hdc \
  --device=/dev/davinci0 \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro \
  -v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/:ro \
  -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi:ro \
  -v /var/log/npu/:/usr/slog \
  "${IMAGE_NAME}" \
  bash -c "npu-smi info && python -c 'import motor; print(\"motor ok\")'"
```

#### Start Inference Service

Prepare `boot.sh`, `user_config.json`, and other config files before deployment. See the [docker-only single-container deployment guide](../docs/zh/user_guide/deployment/docker/single_container.md) for the full workflow.

```bash
CONFIGMAP_PATH="/path/to/configmap"
IMAGE_NAME="mindie-motor-vllm:3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64"

docker run -u root --rm --name mindie-motor \
  -e ASCEND_RUNTIME_OPTIONS=NODRV \
  -e CONFIGMAP_PATH="${CONFIGMAP_PATH}" \
  -e CONFIG_PATH=/usr/local/Ascend/pyMotor/conf \
  -e ROLE=SINGLE_CONTAINER \
  --device=/dev/davinci_manager \
  --device=/dev/devmm_svm \
  --device=/dev/hisi_hdc \
  --device=/dev/davinci0 \
  --device=/dev/davinci1 \
  -p 1025:1025 \
  -p 1026:1026 \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
  -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
  -v /usr/local/sbin:/usr/local/sbin \
  -v /var/log/npu/:/usr/slog \
  -v /mnt:/mnt \
  -v "${CONFIGMAP_PATH}:${CONFIGMAP_PATH}" \
  "${IMAGE_NAME}" \
  bash -c 'export POD_IP=$(grep $(hostname) /etc/hosts | cut -f1) && source ${CONFIGMAP_PATH}/boot.sh'
```

Common parameters:

| Parameter / Env Var | Description |
|---|---|
| `--device=/dev/davinci{N}` | Map NPU devices; add `davinci0`, `davinci1`, etc. as needed |
| `--device=/dev/davinci_manager` etc. | Atlas management devices, usually required |
| `-v /usr/local/Ascend/driver:...` | Mount host Atlas driver directory |
| `-v ${CONFIGMAP_PATH}:...` | Mount startup scripts and config directory |
| `-p <host>:<container>` | Expose API ports per `user_config.json` |
| `ASCEND_RUNTIME_OPTIONS=NODRV` | Reuse host drivers without reinstalling inside the container |
| `CONFIGMAP_PATH` | Path to startup scripts inside the container |
| `CONFIG_PATH` | Motor config directory, default `/usr/local/Ascend/pyMotor/conf` |
| `ROLE` | Deployment role; use `SINGLE_CONTAINER` for single-container PD separation |

### Extend the Image

```bash
FROM mindie-motor-vllm:3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64

RUN apt update -y && \
    apt install gcc ...
```

---

## Supported Hardware

| Chip Series | Product Examples | Architecture |
|---|---|---|
| Atlas 910 | Atlas 800T A2, Atlas 900 A2 PoD | ARM64 / x86_64 |
| Atlas A3 | Atlas 800T A3 | ARM64 / x86_64 |

---

## Image Version Notes

| Version | Description | Notes |
| - | - | - |
| 3.0.0 | MindIE 3.0.0 Release | 2026/5/6: initial release |

## License

See the [Motor license](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/LICENSE.md) for license information included in these images.

As with all container images, pre-installed software packages (Python, system libraries, etc.) may be subject to their own licenses.
