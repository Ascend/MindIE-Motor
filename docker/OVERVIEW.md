# MindIE-Motor

> English | [中文](./OVERVIEW.zh.md)

## Quick Reference

- MindIE-Motor is maintained by the [MindIE community](https://www.hiascend.com/eng/developer/software/mindie)

- Where to get help

    - [AscendHub Image Registry](https://www.hiascend.com/en/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4)
    - [MindIE-Motor Documentation](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/index.md)
    - [Atlas Developer Community](https://www.hiascend.com/en)
    - [Report an Issue](https://gitcode.com/Ascend/MindIE-Motor/issues)

## MindIE-Motor

Provides one-click PD-separated deployment, flexibly adapts to multiple inference engines (vLLM, SGLang) through a cloud-native plug-in architecture, and combines high-performance scheduling with load balancing capabilities to build highly available, scalable large-scale inference services.

## Supported Tags and Dockerfile Links

Official published images are named `mindie-motor` and hosted on [AscendHub mindie-motor](https://www.hiascend.com/en/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4). Each tag is a multi-arch image (`arm64` / `x86_64`).

### Tag Specification

Current published image tags follow these formats:

```text
<MotorVersion>-vllm_ascend<EngineVersion>-<ChipSeries>-<OperatingSystem>-<PythonVersion>
<MotorVersion>-sglang<EngineVersion>-<ChipSeries>-<OperatingSystem>-<PythonVersion>
```

| Field | Example Value | Description |
|---|---|---|
| `MotorVersion` | `3.2.0`, `3.2.0b6` | MindIE-Motor version number |
| `EngineVersion` | `0.5.21`, `0.30.0` | Matching SGLang or vllm-ascend version |
| `ChipSeries` | `a3`, `a5`, `a2` | Target Atlas chip series |
| `OperatingSystem` | `ubuntu22.04`, `openeuler24.03` | Base OS |
| `PythonVersion` | `py3.12` | Python version |

Dedicated images follow this format. `vllm_ascend` is the inference engine, and the model name follows it:

```text
vllm_ascend_<ModelName>-<ChipSeries>-<OperatingSystem>-<PythonVersion>
```

| Field | Example Value | Description |
|---|---|---|
| `ModelName` | `deepseek_v4.1_flash`, `kimi_k3` | Model name. The `vllm_ascend_` prefix is the inference engine, not part of the model name |
| `ChipSeries` | `a2`, `a3` | Target Atlas chip series |
| `OperatingSystem` | `ubuntu22.04`, `openeuler24.03` | Base OS |
| `PythonVersion` | `py3.12` | Python version |

3.0.x historical tags use a different naming scheme; see [Supported Tags](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/supported_tags.md).

### Latest Version MindIE-Motor 3.2.0

The following tables list vLLM-Ascend and SGLang images for the latest MindIE-Motor 3.2.0 release on AscendHub (2026/10/30). For historical tags, see [Supported Tags](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/supported_tags.md).

#### vLLM-Ascend Images

These images are released to pair with vLLM-Ascend v0.30.0.

| Tag | Dockerfile | Architecture | Image Contents |
|---|---|---|---|
| `3.2.0-vllm_ascend0.30.0-a2-ubuntu22.04-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / vllm-ascend 0.30.0 |
| `3.2.0-vllm_ascend0.30.0-a2-openeuler24.03-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / vllm-ascend 0.30.0 |
| `3.2.0-vllm_ascend0.30.0-a3-ubuntu22.04-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / vllm-ascend 0.30.0 |
| `3.2.0-vllm_ascend0.30.0-a3-openeuler24.03-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / vllm-ascend 0.30.0 |
| `3.2.0-vllm_ascend0.30.0-a5-ubuntu22.04-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / vllm-ascend 0.30.0 |
| `3.2.0-vllm_ascend0.30.0-a5-openeuler24.03-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / vllm-ascend 0.30.0 |

#### SGLang Images

These images are released to pair with SGLang v0.5.21.

| Tag | Dockerfile | Architecture | Image Contents |
|---|---|---|---|
| `3.2.0-sglang0.5.21-a3-ubuntu22.04-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / SGLang 0.5.21 |
| `3.2.0-sglang0.5.21-a5-ubuntu22.04-py3.12` | Dockerfile 待上传 | arm64 / x86_64 | motor 3.2.0 / SGLang 0.5.21 |

## Quick Start

### Prerequisites (Optional)

- Firmware and drivers have been installed on the host. Refer to [Environment Setup](https://www.hiascend.com/document/detail/en/mindie/310/servicedeploy/servicedev/docs/en/user_guide/environment_preparation.md) for details.
- Docker and Kubernetes have been installed on the host.

### Using Motor

See the [quick start guide](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/user_guide/quick_start.md).

### Build Locally

Each Dockerfile clones the pinned branch and commit during the build, then runs `build.sh` to install `motor` and `ccae_reporter`. **No local source tree or build context is required.** Replace `<tag>` with the desired combination and run from the repository root:

```bash
TAG="3.2.0-sglang0.5.21-a3-ubuntu22.04-py3.12"

docker build --network=host \
    --platform=linux/arm64 \
    -t "mindie-motor:${TAG}" \
    -f "docker/mindie-motor-vllm/${TAG}/Dockerfile" \
    .
```

Each Dockerfile header comment contains the exact `--platform` value, source repository info, and full `docker build` command.

The `master` Dockerfile installs gcc / curl / libzmq so `build.sh` can locate or rustup-install cargo and pack both `libmindie_workload_shm.so` (required; missing library refuses the wheel) and `kv-conductor` (when libzmq is present). Default `SKIP_WORKLOAD_SHM_BUILD=0` and `SKIP_KV_CONDUCTOR_BUILD=0` force an in-image cargo rebuild even if the build context already contains a host-built `.so` or binary (glibc/ABI). Local `bash build.sh` reuses those files when they already exist. `build.sh` auto-skips kv-conductor if libzmq/pkg-config is missing; it does not apt-install libzmq. Override with `--build-arg SKIP_KV_CONDUCTOR_BUILD=1` to skip even when headers exist. Build the `.so` on the same OS/glibc as the runtime image.

---

## License

See the [Motor license](https://gitcode.com/Ascend/MindIE-Motor/blob/master/LICENSE.md) for license information included in these images.

As with all container images, pre-installed software packages (Python, system libraries, etc.) may be subject to their own licenses.
