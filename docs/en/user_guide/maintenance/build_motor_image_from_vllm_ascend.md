# MindIE Motor Image Installation

## Usage Instructions

Before using MindIE Motor, prepare the image first and **load the same image to all nodes in the K8s cluster**.

You can obtain the image in the following **two ways**:

- Method 1: [Using the MindIE Motor official full image](#using-the-official-full-mindie-motor-image)

- Method 2: [Installing MindIE Motor based on the vllm-ascend/sglang image](#installing-mindie-motor-based-on-the-vllm-ascendsglang-image)

To **upgrade or uninstall** MindIE Motor, refer to:

- [MindIE Motor Upgrade](#upgrading-mindie-motor)

- [MindIE Motor Uninstallation](#uninstalling-mindie-motor)

## Using the Official Full MindIE Motor Image

1. Go to the [Ascend official image repository](https://www.hiascend.com/developer/ascendhub), search for `motor`, and select the corresponding MindIE Motor image based on the device model.

2. After obtaining the image, use the following command to load the image to all servers in the k8s cluster.

     ```bash
     docker load -i xxxx.tar
     ```

3. After the image is imported, use the following command to check whether the docker image exists.

     ```bash
     docker images
     ```

4. After the image is prepared, refer to [PD Disaggregation Deployment Guide](../deployment/k8s/pd_disaggregation_deployment.md) or [PD Co-location Deployment Guide](../deployment/k8s/pd_aggregation_deployment.md) to deploy the service.

## Installing MindIE Motor Based on the vllm-ascend/sglang Image

### (Optional) Downloading Dependencies

>[!NOTE]NOTE
>If the machine used to create the image can access the Internet, go to [Obtaining the Base Image](#obtaining-the-base-image-using-vllm-ascend-as-an-example). If the server used to create the image cannot access the network, perform the steps in this section.

Perform the following steps in an environment with network access:

1. Download pciutils.

     ```sh
     mkdir -p /mnt/pciutils-offline
     cd /mnt/pciutils-offline

     apt-get install -y apt-rdepends
     apt-get download $(apt-rdepends pciutils | grep -v "^ ")

     cd /mnt/
     tar -czvf pciutils-offline.tar.gz pciutils-offline
     ```

     Copy `/mnt/pciutils-offline.tar.gz` to the `/mnt/` path on the machine used to create the image.

2. Download the .whl dependencies.

     Download the MindIE Motor source code to the `/mnt/` path.

     ```bash
     cd /mnt/
     git clone <motor git link>

     mkdir -p /mnt/packages-offline

     # The image already includes Transformers. Delete this dependency before downloading to avoid version conflicts
     sed -i '/^transformers/d' /mnt/MindIE-Motor/requirements.txt

     pip download -r /mnt/MindIE-Motor/requirements.txt -d /mnt/packages-offline -i https://pypi.tuna.tsinghua.edu.cn/simple

     cd /mnt/
     tar -czvf packages-offline.tar.gz packages-offline
     ```

     Copy `/mnt/packages-offline.tar.gz` to the `/mnt/` path on the machine used to create the image.

3. Build the .whl package of MindIE Motor.

     ```bash
     cd /mnt/MindIE-Motor

     # The built .whl package is under the /mnt/MindIE-Motor/dist/ path

     bash build.sh

     cd /mnt/
     tar -czvf MindIE-Motor.tar.gz MindIE-Motor
     ```

Copy `/mnt/MindIE-Motor.tar.gz` to the `/mnt/` path of the image machine.

### Obtaining the Base Image (Using vLLM-Ascend as an Example)

Obtaining method: Open [RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags) and click the version you need to download. Using v0.13.0 as an example, the download command is:

```bash
docker pull quay.io/ascend/vllm-ascend:v0.13.0
```

>[!NOTE]NOTE
>To improve the download speed, you can replace `quay.io` with `quay.nju.edu.cn`.

### Installing MindIE Motor

1. View the image.

     ```bash
     docker images
     ```

2. Create a container and mount the `mnt` directory.

     ```bash
     docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <image name>
     ```

3. Start the container.

     ```bash
     docker start docker-vllm-ascend
     ```

4. Enter the container.

     ```bash
     docker exec -it docker-vllm-ascend bash
     ```

5. Install MindIE Motor and its dependencies.

     - Install pciutils

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

     - Install the .whl dependencies

       - Online installation:

         ```bash
         # Download the motor code and run the following commands. Modify the git command based on the branch or tag to be downloaded
         cd /mnt/
         git clone <motor git link>

         cd /mnt/MindIE-Motor

         # The image already includes Transformers. Delete this dependency before installation to avoid version conflicts
         sed -i '/^transformers/d' requirements.txt

         pip install -r requirements.txt

         bash build.sh
         pip install --force-reinstall ./dist/motor-*.whl

         mkdir -p /tmp/motor/
         cp -r ./examples/ /tmp/motor/

         # Exit the container
         exit
         ```

       - Offline installation:

         ```bash
         # Install the .whl dependencies
         cd /mnt/
         tar -xzvf packages-offline.tar.gz
         pip install /mnt/packages-offline/*.whl --force-reinstall --no-index -v

         # Install motor
         pip install --force-reinstall /mnt/MindIE-Motor/dist/motor-*.whl --no-index -v

         # Copy the examples
         mkdir -p /tmp/motor/
         cp -r /mnt/MindIE-Motor/examples/ /tmp/motor/

         # Exit the container
         exit
         ```

6. Save the image.

     ```bash
     docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
     ```

     After saving, the `mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64` image is the created image with MindIE Motor.

7. Package the image.

     ```bash
     docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
     ```

8. Import the image with MindIE Motor.

     Install the image on all K8s servers.

     ```bash
     docker load -i /mnt/motor-vllm-ascend.tar
     ```

9. The image preparation is complete. You can refer to [PD Disaggregation Deployment Guide](../deployment/k8s/pd_disaggregation_deployment.md) or [PD Co-location Deployment Guide](../deployment/k8s/pd_aggregation_deployment.md) to deploy the service.

## Upgrading MindIE Motor

Refer to all content in the [Installing MindIE Motor Based on the vllm-ascend/sglang Image](#installing-mindie-motor-based-on-the-vllm-ascendsglang-image) section and reinstall MindIE Motor. In the [Obtaining the Base Image (Using vllm-ascend as an Example)](#obtaining-the-base-image-using-vllm-ascend-as-an-example) section, use the MindIE Motor image that you want to upgrade as the base image, and keep the remaining steps unchanged.

## Uninstalling MindIE Motor

1. Create a container based on the image of the MindIE Motor to be uninstalled, and run the following command to enter the container.

     ```bash
     docker exec -it <container name> bash
     ```

2. Confirm that MindIE Motor is installed in the current environment.

     ```bash
     pip show motor
     ```

3. Perform the uninstallation and exit the current container.

     ```bash
     pip uninstall motor -y
     exit
     ```

4. Commit the container as a new image. The MindIE Motor is uninstalled.

     ```bash
     docker commit <container name>  <new image name>
     ```
