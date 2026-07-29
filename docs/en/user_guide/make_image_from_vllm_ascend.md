# Image Creation

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:05:29.668Z pushedAt=2026-07-01T08:50:39.335Z -->

## Obtaining the vLLM-Ascend Released Image Version

How to obtain: Open [RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags) and click the version you need to download. Taking version v0.13.0 as an example, the download command is:

```bash
docker pull quay.io/ascend/vllm-ascend:v0.13.0
```

>[!NOTE]Note
>To increase the download speed, you can replace `quay.io` with `quay.nju.edu.cn`.

## Installing PyMotor in the Image

1. Prepare the target PyMotor code and run the following command. Modify the git command based on the branch or tag you need to download.

    ```bash
    cd /mnt/
    git clone <git link for PyMotor>
    ```

2. Run the following command to view the image downloaded in the first step:

    ```bash
    docker images
    ```

3. Run the following command to start the container and mount the mnt directory:

    ```bash
    docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <image name>
    ```

4. Run the following commands to build the image:

    ```bash
    apt-get update && apt-get install pciutils -y

    cd /mnt/MindIE-PyMotor
    ```

    ```bash
    docker exec -it docker-vllm-ascend bash

    cd /mnt/MindIE-PyMotor

    pip install -r requirements.txt

    bash build.sh

    pip install --force-reinstall ./dist/motor-0.1.0-py3-none-any.whl

    exit
    ```

5. Run the following command to save the image:

    ```bash
    docker commit -m "add PyMotor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
    ```

    After saving, the `mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64` image is the image with PyMotor installed.
