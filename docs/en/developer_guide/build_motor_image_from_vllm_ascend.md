# Installing MindIE Motor Based on vllm-ascend

Build a wheel that contains `libmindie_workload_shm.so` first, and then install it into the image. **The first packaging depends on the Rust toolchain (rustc + cargo)**: `bash build.sh` automatically detects an existing cargo, and installs it online through rustup (using the domestic rsproxy mirror by default) when cargo is not found and the `.so` is missing. For details, see "Build Dependency: Rust Toolchain" below. If `lib/*.so` or `bin/kv-conductor` already exists, the corresponding cargo build is skipped, and it is performed only when the file is missing. The wheel must always contain workload-shm. kv-conductor is built only when cargo + libzmq are available and the conductor binary is missing, and it is skipped automatically when libzmq is missing (you can also explicitly run `SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh`). After modifying `.rs` files, use `SKIP_WORKLOAD_SHM_BUILD=0` / `SKIP_KV_CONDUCTOR_BUILD=0` to force a rebuild; for offline environments, use `WORKLOAD_SHM_PREBUILT`. When the `.so` is missing, `build.sh` refuses to produce a package. The official Dockerfile sets both SKIP variables to `0` by default, and compilation always targets the ABI inside the image.

## Building a Development and Test Image

The project provides `docker/mindie-motor-vllm/master/Dockerfile`, which is used to build the source code of the current workspace into the vLLM-Ascend base image. This Dockerfile differs from the release image Dockerfile in purpose:

| Type | Path | Source Code Source | Applicable Scenario |
|---|---|---|---|
| Development image | `docker/mindie-motor-vllm/master/Dockerfile` | Current workspace | master branch, local changes, and CI verification |
| Release image | `docker/mindie-motor-vllm/<tag>/Dockerfile` | Fixed branch and commit | Reproducible build of a release version |

### Local Single-Architecture Build

Run the following in the project root directory:

```bash
make build-pymotor-image
```

This command uses the Atlas 800I A2 Ubuntu base image by default, builds a `linux/arm64` image, and loads it as `mindie-motor-vllm:master` through `type=docker`. In an Atlas 800I A3 development environment, you can run:

```bash
make build-pymotor-image \
  BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3 \
  PLATFORMS=linux/arm64 \
  TAG=master-a3
```

### Multi-Architecture Build and Push

The same `build-pymotor-image` target also supports multi-architecture builds. When pushing to an image repository, set `OUTPUT` to `type=registry` and provide `REGISTRY`:

```bash
docker login example.com

make build-pymotor-image \
  REGISTRY=example.com/team \
  TAG=master \
  PLATFORMS=linux/amd64,linux/arm64 \
  OUTPUT=type=registry
```

The final image name is `example.com/team/mindie-motor-vllm:master`.

> [!NOTE]NOTE
> `type=docker` can load only a single-architecture image into the local Docker. When `PLATFORMS` contains multiple platforms, use `OUTPUT=type=registry` to push a multi-architecture image.

### Make Variables

| Variable | Default Value | Description |
|---|---|---|
| `DOCKERFILE` | `docker/mindie-motor-vllm/master/Dockerfile` | Dockerfile used for the build |
| `BASE_IMAGE` | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0` | vLLM-Ascend base image |
| `IMAGE_NAME` | `mindie-motor-vllm` | Image name |
| `TAG` | `master` | Image tag |
| `REGISTRY` | Empty | Repository and namespace for multi-architecture push |
| `PLATFORMS` | `linux/arm64` | Build platforms; separate multiple platforms with commas |
| `PIP_INDEX_URL` | Huawei Cloud PyPI mirror | Python software source |
| `PIP_TRUSTED_HOST` | `repo.huaweicloud.com` | Trusted host of the Python software source |
| `OUTPUT` | `type=docker` | buildx output type; use `type=registry` for multi-architecture push |

Commonly used base images are as follows:

| Hardware | Operating System | `BASE_IMAGE` |
|---|---|---|
| Atlas 800I A2 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0` |
| Atlas 800I A3 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3` |
| Atlas 800I A2 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-openeuler` |
| Atlas 800I A3 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3-openeuler` |

The Dockerfile build process includes:

1. Install `pciutils` with `apt-get`, `dnf`, or `yum` depending on the base operating system.
2. Install the Python dependencies of the project.
3. Build and install the `motor` wheel package from the current workspace.
4. Build and install the `ccae_reporter` observability component.
5. Copy the examples to `/tmp/motor/examples`, and generate the container entrypoint script and usage agreement.

> [!NOTE]NOTE
> Uncommitted changes in the current workspace are also copied into the image. Development builds do not pin the source code commit; for formal delivery, use `docker/mindie-motor-vllm/<tag>/Dockerfile`.

## Manual Installation and Offline Build

### Build Dependency: Rust Toolchain

`bash build.sh` packages `libmindie_workload_shm.so` into the wheel (**mandatory**; Coordinator has no Python ledger fallback, and packaging is refused directly when the library is missing): if `lib/*.so` already exists, cargo is skipped, and the build is performed only when it is missing. When `bin/kv-conductor` is missing and cargo + libzmq are available, kv-conductor is also built (optional). The official Dockerfile sets `SKIP_WORKLOAD_SHM_BUILD=0` and `SKIP_KV_CONDUCTOR_BUILD=0` by default, and compilation inside the image always targets the ABI. Therefore, the container used to build the image needs the **Rust toolchain (rustc + cargo)**, as well as the C toolchain and dependency libraries required for compilation:

| Component | Purpose | Mandatory |
|---|---|---|
| gcc / g++ / build-essential (or `gcc gcc-c++ make`) | Linking the cdylib; `zmq-sys` of kv-conductor requires `c++` | Mandatory (g++ is mandatory when kv-conductor is built) |
| curl or wget | Downloading the rustup installation script | Mandatory when cargo is unavailable |
| Rust (rustc + cargo, stable recommended) | Compiling the `workload_shm_rs` and `kv_conductor` crates | Mandatory |
| libzmq headers + pkg-config (Ubuntu: `libzmq3-dev pkg-config`; openEuler: `zeromq-devel pkgconf`) | Compiling the optional `kv-conductor` component | Mandatory only when kv-conductor is packaged |

**Installing Rust Online (the domestic rsproxy mirror is recommended):**

```bash
export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y
source "$HOME/.cargo/env"

rustup --version && rustc --version && cargo --version
```

`build.sh` performs the same detection/installation itself: it first looks for an existing cargo in `PATH` / `$HOME/.cargo` / `CARGO_HOME` (`scripts/ensure_rust.sh`), and only when cargo is not found does it use the rustup installation above (rsproxy by default; you can use `RUSTUP_DIST_SERVER` / `RUSTUP_UPDATE_ROOT` / `RUSTUP_INIT_URL` to switch to another mirror source). Therefore, as long as the container used to build the image can access the network and has `curl` and a C compiler installed, running `bash build.sh` directly installs Rust automatically, and manual pre-installation is not required.

**Offline environment (the container cannot access the network and rustup online download must be disabled):**

1. Compile the `.so` on a machine with network access (the OS/glibc must be the same as that of the runtime image), and then copy it in directly with `WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so bash build.sh`; similarly, `KV_CONDUCTOR_PREBUILT=/path/to/kv-conductor` can be used when packaging kv-conductor.
2. Alternatively, put the compiled `.so` into `motor/coordinator/workload_shm_rs/lib/` in advance (and `kv-conductor` into `motor/kv_conductor/bin/`), and run `SKIP_RUST_INSTALL=1 bash build.sh` to disable the rustup online installation; `build.sh` reuses the existing artifacts directly.
3. If neither is available, `build.sh` runs `exit 1` before compilation and prints `refusing to emit dist/motor-*.whl: ...`, and no wheel with missing libraries is produced.

When libzmq is missing, `build.sh` detects it and automatically skips the optional kv-conductor (it does not fail because of this, and libzmq is not installed by the script); you can still explicitly skip it with `SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh`. workload-shm is still compiled as usual and is not affected. For details about the switches and their priority, see the "Build" section of `AGENTS.md` in the repository root.

## (Optional) Downloading Dependencies

>[!NOTE]NOTE
>If the machine used to build the image cannot access the network, download the dependencies first.

Perform the following steps in an environment with network access:

### Downloading pciutils

```sh
mkdir -p /mnt/pciutils-offline
cd /mnt/pciutils-offline

apt-get install -y apt-rdepends
apt-get download $(apt-rdepends pciutils | grep -v "^ ")

cd /mnt/
tar -czvf pciutils-offline.tar.gz pciutils-offline
```

Copy `/mnt/pciutils-offline.tar.gz` to the `/mnt/` path on the machine used to build the image.

### Downloading the Rust Toolchain (Mandatory for Offline Environments)

The next step, "Building the MindIE Motor .whl Package", uses `bash build.sh` to compile `libmindie_workload_shm.so`. If the machine used to build the image cannot access the network, download the rustup installer and the offline Rust toolchain package in an environment with network access in advance:

```sh
mkdir -p /mnt/rust-offline
cd /mnt/rust-offline

# The rustup-init binary itself (select it based on the target machine architecture; aarch64 Linux is used as an example here)
curl --proto '=https' --tlsv1.2 -sSf -o rustup-init \
  https://rsproxy.cn/rustup-init.sh

# Download the stable toolchain components to the local host in rustup offline archive mode, for installation on the machine without network access
RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup \
  rustup toolchain install stable --profile minimal

tar -czvf /mnt/rust-offline.tar.gz -C "$HOME" .cargo .rustup
```

Copy `/mnt/rust-offline.tar.gz` and `rustup-init` to the `/mnt/` path on the machine used to build the image. After extracting them and placing them in `$HOME` on the machine used to build the image, they can be used directly (`build.sh` automatically detects `$HOME/.cargo`, so there is no need to run the rustup installation again):

```bash
tar -xzvf /mnt/rust-offline.tar.gz -C "$HOME"
source "$HOME/.cargo/env"
rustc --version && cargo --version
```

If the machine used to build the image does not have any compilation capability at all, you can also compile the `.so` (and the optional `kv-conductor` binary) directly on **another machine with the same OS/glibc as that of the runtime image**, copy it together with the offline package, use `WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so` when building the whl (and `KV_CONDUCTOR_PREBUILT=/path/to/kv-conductor` for the optional component), and set `SKIP_RUST_INSTALL=1` for `build.sh` to disable the rustup online installation.

### Downloading .whl Dependencies

Download the MindIE Motor source code to the `/mnt/` directory.

```bash
cd /mnt/
git clone <MindIE Motor git link>

mkdir -p /mnt/packages-offline

# The image already includes Transformers. Remove this dependency before downloading to avoid version conflicts
sed -i '/^transformers/d' /mnt/MindIE-Motor/requirements.txt

pip download -r /mnt/MindIE-Motor/requirements.txt -d /mnt/packages-offline -i https://pypi.tuna.tsinghua.edu.cn/simple

cd /mnt/
tar -czvf packages-offline.tar.gz packages-offline
```

Copy `/mnt/packages-offline.tar.gz` to the `/mnt/` directory of the machine used to build the image.

### Building the MindIE Motor .whl Package

```bash
cd /mnt/MindIE-Motor

# The built .whl package is located in the /mnt/MindIE-Motor/dist/ directory
# Run this in an OS container that is the same as the runtime image (gcc/curl required; when cargo is unavailable, build.sh installs rustup automatically,
# for offline environments use the /mnt/rust-offline.tar.gz downloaded in the previous step, or pass the precompiled .so directly through WORKLOAD_SHM_PREBUILT)
# When libzmq is missing, build.sh automatically skips kv-conductor; explicit skipping is still available:
SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh

cd /mnt/
tar -czvf MindIE-Motor.tar.gz MindIE-Motor
```

Copy `/mnt/MindIE-Motor.tar.gz` to the `/mnt/` directory of the machine used to build the image.

## Obtaining the Base Image (Using vLLM-Ascend as an Example)

>[!NOTE]NOTE
>To improve the download speed, you can replace `quay.io` with `quay.nju.edu.cn`.

Obtaining method: Open [RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags) and click the version you want to download.
Taking v0.13.0 as an example, the download command is:

```bash
docker pull quay.io/ascend/vllm-ascend:v0.13.0
```

## Installing MindIE Motor

### Viewing the Image

```bash
docker images
```

### Creating a Container and Mounting the mnt Directory

```bash
docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <image name>
```

### Starting the Container

```bash
docker start docker-vllm-ascend
```

### Entering the Container

```bash
docker exec -it docker-vllm-ascend bash
```

### Installing MindIE Motor and Its Dependencies

#### Installing pciutils

- Online installation:

```bash
apt-get update && apt-get install pciutils -y
```

- Offline installation:

```sh
cd /mnt/
tar -xzvf pciutils-offline.tar.gz
cd pciutils-offline

dpkg -i *.deb
```

#### Installing the .whl Dependencies

- Online installation:

    ```bash
    # Download the MindIE Motor code and run the following commands. Modify the git command based on the branch or tag to be downloaded
    cd /mnt/
    git clone <MindIE Motor git link>

    cd /mnt/MindIE-Motor

    # The image already includes transformers. Remove this dependency before installation to avoid version conflicts
    sed -i '/^transformers/d' requirements.txt

    pip install -r requirements.txt

    # When libzmq is available, bash build.sh builds kv-conductor; it is skipped automatically when zmq is missing. To skip explicitly:
    SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh
    pip install --force-reinstall ./dist/motor-*.whl

    mkdir -p /tmp/motor/
    cp -r ./examples/ /tmp/motor/

    # Exit the container
    exit
    ```

- Offline installation

    ```bash
    # Install the whl dependencies
    cd /mnt/
    tar -xzvf packages-offline.tar.gz
    pip install /mnt/packages-offline/*.whl --force-reinstall --no-index -v

    # Install MindIE Motor
    pip install --force-reinstall /mnt/MindIE-Motor/dist/motor-*.whl --force-reinstall --no-index -v

    # Copy the examples
    mkdir -p /tmp/motor/
    cp -r /mnt/MindIE-Motor/examples/ /tmp/motor/

    # Exit the container
    exit
    ```

### Saving the Image

```bash
docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
```

After saving, the `mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64` image is the image that contains MindIE Motor after it has been created.

### Packaging the Image

```bash
docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
```

### Importing the Image with MindIE Motor

Import the image on a node other than the one used to build the image.

```bash
docker load -i /mnt/motor-vllm-ascend.tar
```
