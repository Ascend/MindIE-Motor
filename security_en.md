# Security Statement

## Security Precautions

When using MindIE Motor, to ensure security, users should review the network security hardening measures of the entire system based on their own business and perform relevant configurations according to the security policies of their organization, including but not limited to software versions, password complexity requirements, security configurations (protocols, cipher suites, key lengths, etc.), permission configurations, and firewall settings. For more security declarations and recommendations, see [MindIE Security Management and Hardening on Ascend Community](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0041.html), subject to the latest version on the community.

## Recommended Operating Environment

- To reduce potential security risks, use non-root, non-administrator accounts to perform system operations. Ensure that only root is the highest-privilege user of the system, that the UIDs of all system accounts are different, and that the principle of least privilege is followed.
- Perform antivirus scans on the cluster on a regular basis. Routine antivirus checks help protect the cluster from viruses, malicious code, spyware, and programs, reducing risks such as system crashes and information leakage. You can use mainstream antivirus software in the industry for antivirus checks.
- To ensure the security of the production environment and reduce the risk of attacks, regularly check [MindIE Security Management and Hardening on Ascend Community](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0041.html) to fix vulnerabilities and functional issues.

## File Permission Control

- Set the umask of hosts (including the host machine) and containers to 0027 or higher to improve security.
- Control access permissions for files containing sensitive content, such as personal privacy data, commercial assets, and business development-related files. For example, for the installation directory permission control and data file permission control in this project, refer to [A-Recommended Maximum Permission Control Values for Files (Folders) in Each Scenario](#a-recommended-maximum-permission-control-values-for-files-folders-in-each-scenario) for the permissions to be set.
- The use of shell scripts with special permissions such as SetUID or SetGID is prohibited.
- The use of executable files with high-risk capabilities is prohibited.
- Files without an owner are not allowed in the system.

## Build Security Statement

- This project needs to be compiled and built to produce packages. The compilation process generates some intermediate files and build directories. Control the permissions of these files properly. During the build process, modify the build scripts as needed to avoid related security risks, and pay attention to the security of the build results.
- This project involves Python whl package installation. To prevent risks such as code tampering and forgery caused by other users directly accessing and modifying Python code, it is recommended that you set Python to be modifiable and usable only by the installing user.
- Use the ASLR (Address Space Layout Randomization) and KASLR (Kernel Address Space Layout Randomization) mechanisms built into Linux for secure compilation.
    - ASLR, once enabled, can enhance vulnerability attack protection. The enabling method is as follows:

        ```shell
        echo 2 > /proc/sys/kernel/randomize_va_space
        ```

    - KASLR, once enabled, can increase the difficulty of attacks targeting kernel vulnerabilities. The enabling method is as follows:
    1. Use the following example command to view the kernel configuration file.

        ```shell
        vi /boot/config-$(uname -r)
        ```

        If the following line exists, KASLR is supported.

        ```shell
        CONFIG_RANDOMIZE_BASE=y
        ```

    2. Open the configuration file /etc/default/grub and add the kaslr parameter to the line where GRUB_CMDLINE_LINUX_DEFAULT is located. An example is as follows.

        ```shell
        GRUB_CMDLINE_LINUX_DEFAULT="kaslr"
        ```

    3. Use the following command to update the grub configuration.

        ```shell
        sudo update-grub
        ```

    4. Use the following command to restart the system, after which the KASLR feature is enabled.

        ```shell
        sudo reboot
        ```

- To prevent buffer overflow attacks, it is recommended to use the ASLR technology. By randomizing the layout of linear regions such as the heap, stack, and shared library mappings, it increases the difficulty for attackers to predict the target address and prevents attackers from directly locating the attack code position. This technology can be applied to the heap, stack, and memory mapping regions (mmap base address, shared libraries, and vdso pages).
    1. Ensure that the current user has write permission on the "/proc/sys/kernel/randomize_va_space" file.
    2. Enable buffer overflow security protection.

        ```shell
        echo 2 >/proc/sys/kernel/randomize_va_space
        ```

## Data Security Statement

- This project involves receiving input, loading model weights, and saving result data. Some interfaces directly or indirectly use the risky module pickle, which may pose data risks. Ensure that the input data source and the save path address are trusted. When loading model weights, it is recommended to use local weights.

## Runtime Security Statement

- To prevent information leakage during communication between the service and clients, you are advised to enable HTTPS communication and mutual authentication. If enabled, you are advised to implement proper security access control for the certificates, private keys, and passwords involved in communication authentication.
- MindIE Motor provides only partial flow control capabilities and does not directly connect to the public network. You are advised to properly control MindIE Motor flow control and isolate the public network from the LAN. For example, you can use the open-source software Nginx for protection. For Nginx deployment, refer to the [Nginx official documentation](https://nginx.org/en/docs/) and [Ascend Community Server security hardening](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0068.html).
- For ports listening on all networks and other ports, you are advised to close them if they are not necessary.
- You are advised to disable insecure services such as Telnet and FTP.
- Based on your own services, you can limit the connection rate to the server by IP address to protect the system against DoS attacks. Methods include but are not limited to using the iptables firewall built into the Linux system for prevention and optimizing sysctl parameters.
- The default Gloo, DataDist, and HCCL communication of this project does not support TLS authentication. If needed, refer to [B-Collective Communication Hardening](#b-collective-communication-hardening).

## Public Interface Statement

All external interfaces provided by this project have been disclosed in the documentation. You are advised to directly use the public interfaces described in the documentation and not to directly call interface source code that is not explicitly disclosed.

## Communication Matrix

The communication matrix of this project, including the ports opened by the product, the transport layer protocol used by each port, the name of the communication network element that communicates with the peer through the port, the authentication method, the purpose, and other information, has been disclosed in the documentation. For details, see [Ascend Community MindIE Communication Matrix](https://www.hiascend.com/document/detail/en/mindie/22RC1/ref/commumatrix/Communication0000.html). The latest community version prevails.

## Public IP Address Statement

The public network address declarations contained in the code of this project have been disclosed in the documentation. For details, see [Ascend Community MindIE Public Network URL](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0089.html). The latest community version prevails.

## Vulnerability Mechanism Description

[Vulnerability Management](https://gitcode.com/Ascend/community/blob/master/docs/security.md)

## Disclaimer

- This project is intended for debugging and development purposes only. Users shall use it at their own risk and understand the following:

  - [X] Data processing and deletion: Data generated by users during the use of this project (including but not limited to inference results and logs) falls within the scope of user responsibility. Users are advised to delete relevant data in a timely manner after use to prevent leakage or unnecessary information disclosure.
  - [X] Data confidentiality and dissemination: Users understand and agree that data generated through this project shall not be arbitrarily distributed or disseminated. This project and its developers disclaim all liability for any information leakage, data leakage, or other adverse consequences arising from it.
  - [X] User input security: Users shall ensure the security of the command lines, parameters, and configuration files they enter and shall bear any security risks or losses arising from improper input. This project and its developers disclaim all liability for any issues caused by improper input.

- Scope of disclaimer: This disclaimer applies to all individuals or entities using this project. By using this project, you agree to and accept the contents of this statement and are willing to bear the risks and liabilities arising from the use of this feature. If you have any objection, please stop using this project.
- Before using this project, please **carefully read and understand the contents of the above disclaimer**. For any issues or questions arising from the use of this project, please contact the developers in a timely manner.

## Appendixes

### A-Recommended Maximum Permission Control Values for Files (Folders) in Each Scenario

| Type           | Maximum Reference Linux Permission |
| -------------- | ---------------  |
| User home directory                        |   750(rwxr-x---)            |
| Program files (including script files, library files, etc.)       |   550(r-xr-x---)             |
| Program file directory                      |   550(r-xr-x---)            |
| Configuration file                          |  640(rw-r-----)             |
| Configuration file directory                      |   750(rwxr-x---)            |
| Log file (recording completed or already archived)        |  440(r--r-----)             |
| Log file (currently recording)                |    640(rw-r-----)           |
| Log file directory                      |   750(rwxr-x---)            |
| Debug file                         |  640(rw-r-----)         |
| Debug file directory                     |   750(rwxr-x---)  |
| Temporary file directory                      |   750(rwxr-x---)   |
| Maintenance and upgrade file directory                  |   770(rwxrwx---)    |
| Business data file                      |   640(rw-r-----)    |
| Business data file directory                  |   750(rwxr-x---)      |
| Key component, private key, certificate, and ciphertext file directory    |  700(rwx-----)      |
| Key component, private key, certificate, and encrypted ciphertext        | 600(rw-------)      |
| Encryption/decryption interface and encryption/decryption script            |   500(r-x------)        |

### B-Collective Communication Hardening

The steps for compiling and installing PyTorch with TLS support are as follows.

- Step 1 Compile PyTorch
    1. Compile the PyTorch source code.

        ```shell
        git clone https://github.com/pytorch/pytorch.git --depth=1 -b v2.1.0
        git submodule sync && git submodule update --init --depth=1 --recursive
        ```

    2. Install openssl-1.1

        ```shell
        wget https://www.openssl.org/source/openssl-1.1.1w.tar.gz
        tar -xzf openssl-1.1.1w.tar.gz
        cd openssl-1.1.1w
        ./config --prefix=/usr/local/openssl-1.1
        make -j$(nproc)
        sudo make install
        ```

    3. Export environment variables

        ```shell
        export OPENSSL_ROOT_DIR=/usr/local/openssl-1.1
        export LD_LIBRARY_PATH=$OPENSSL_ROOT_DIR/lib:$LD_LIBRARY_PATH
        export USE_GLOO=1
        export USE_GLOO_WITH_OPENSSL=1
        ```

    4. Build the Python package

        ```shell
        python3 setup.py bdist_wheel
        ```

- Step 2 Install PyTorch. TLS support requires installing torch 2.1.0a0+git7bcf7da.

    ```shell
    cd dist
    pip install --ignore-installed torch-2.1.0a0+git7bcf7da-cp311-cp311-linux_aarch.whl
    ```

- Step 3 Compile and install Gloo

    ```shell
    git clone https://github.com/pytorch/gloo.git
    mkdir build && cd build
    cmake .. -USE_TCP_OPENSSL_LOAD=ON
    make -j&(nproc)
    sudo make install
    export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH
    ```

- Step 4 Enable GLOO TLS

    ```shell
    export GLOO_DEVICE_TRANSPORT=TCP_TLS
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_PKEY=/path/to/tls_ca/server.key.pem
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_CERT=/path/to/tls_ca/server.pem
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_CA_FILE=/path/to/tls_ca/ca.pem
    ```
