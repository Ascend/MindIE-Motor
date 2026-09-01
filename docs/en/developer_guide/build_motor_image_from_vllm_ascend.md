# Installing MindIE Motor Based on the vllm-ascend Image

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
bash build.sh

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

    bash build.sh
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
