# MindIE-Motor

> English | [中文](./OVERVIEW.zh.md)

## Quick Reference

- MindIE-Motor is maintained by the [MindIE community](https://www.hiascend.com/cn/developer/software/mindie)

- Where to get help

    - [AscendHub Image Registry](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4)
    - [MindIE-Motor Documentation](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/index.md)
    - [Atlas Developer Community](https://www.hiascend.com/developer)
    - [Report an Issue](https://gitcode.com/Ascend/MindIE-Motor/issues)

## MindIE-Motor

Provides one-click PD-separated deployment, flexibly adapts to multiple inference engines (vLLM, SGLang) through a cloud-native plug-in architecture, and combines high-performance scheduling with load balancing capabilities to build highly available, scalable large-scale inference services.

## Supported Tags and Dockerfile Links

Official published images are named `mindie-motor` and hosted on [AscendHub mindie-motor](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4). Each tag is a multi-arch image (`arm64` / `x86_64`).

### Tag Specification

Current published image tags follow this format:

```text
<MotorVersion>-<EngineVersion>-<ChipSeries>-<OperatingSystem>-<PythonVersion>
```

| Field | Example Value | Description |
|---|---|---|
| `MotorVersion` | `3.1.0`, `3.1.0b1` | MindIE-Motor version number |
| `EngineVersion` | `0.23.0`, `0.23.0rc1` | Matching vllm-ascend version |
| `ChipSeries` | `a2`, `a3`, `a5` | Target Atlas chip series |
| `OperatingSystem` | `ubuntu22.04`, `openeuler24.03` | Base OS |
| `PythonVersion` | `py3.12` | Python version |

3.0.x historical tags use a different naming scheme; see [Supported Tags](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/supported_tags.md).

### Latest Version MindIE-Motor 3.1.0

The following table lists all images for the latest MindIE-Motor 3.1.0 release on AscendHub (2026/08/18). For historical tags, see [Supported Tags](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/supported_tags.md).

| Tag | Dockerfile | Architecture | Image Contents |
|---|---|---|---|
| `3.1.0-vllm_ascend0.23.0-a2-ubuntu22.04-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a2-ubuntu22.04-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a2-openeuler24.03-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a2-openeuler24.03-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a3-ubuntu22.04-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a3-ubuntu22.04-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a3-openeuler24.03-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a3-openeuler24.03-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a5-ubuntu22.04-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a5-ubuntu22.04-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a5-openeuler24.03-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a5-openeuler24.03-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |

## Quick Start

### Prerequisites (Optional)

- Firmware and drivers have been installed on the host. Refer to [Install Drivers and Firmware](https://www.hiascend.com/document/detail/zh/mindie/100/envdeployment/instg/mindie_instg_0006.html) for details.
- Docker and Kubernetes have been installed on the host.

### Using Motor

See the [quick start guide](../docs/zh/user_guide/quick_start.md).

### Build Locally

Each Dockerfile clones the pinned branch and commit during the build, then runs `build.sh` to install `motor` and `ccae_reporter`. **No local source tree or build context is required.** Replace `<tag>` with the desired combination and run from the repository root:

```bash
TAG="3.1.0-vllm_ascend0.23.0-a2-ubuntu22.04-py3.12"

docker build --network=host \
    --platform=linux/arm64 \
    -t "mindie-motor:${TAG}" \
    -f "docker/mindie-motor-vllm/${TAG}/Dockerfile" \
    .
```

Each Dockerfile header comment contains the exact `--platform` value, source repository info, and full `docker build` command.

---

## License

See the [Motor license](https://gitcode.com/Ascend/MindIE-Motor/blob/master/LICENSE.md) for license information included in these images.

As with all container images, pre-installed software packages (Python, system libraries, etc.) may be subject to their own licenses.
