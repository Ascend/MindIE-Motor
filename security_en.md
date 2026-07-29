# Security Statement

## Security Precautions

When using PyMotor, to ensure security, you should review the network security hardening measures of the entire system based on your business needs and perform related configurations according to the security policies of your organization. The configurations include but are not limited to software versions, password complexity requirements, security configurations (protocols, cipher suites, and key lengths), permission configurations, and firewall settings. For more security statements and suggestions, see [MindIE Security Management and Hardening](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0041.html). The latest version of the community shall prevail.

## Operating Environment Suggestions

- To reduce potential security risks, you are advised to use a non-root or non-administrator account to perform system operations. Ensure that only the root user has the highest permission in the system, each system account has a unique UID, and the principle of least privilege is followed.
- Periodically scan clusters for viruses. This protects clusters from viruses, malicious code, spyware, and malicious programs, reducing risks such as system breakdown and information leakage. Mainstream antivirus software can be used for antivirus check.
- To ensure production environment security and minimize attack risks, periodically review the [MindIE Security Management and Hardening](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0041.html) to address vulnerabilities and functional issues.

## File Permission Control

- You are advised to set `umask` of the host (including the host machine) and container to `0027` or a larger value to improve security.
- You are advised to control the access permissions on files that contain sensitive content related to personal privacy data, business assets, and service development. For example, permissions for the project installation directory and data files must follow the recommendations in [A-Recommended Maximum Permissions for Files and Folders in Different Scenarios](#a-recommended-maximum-permissions-for-files-and-folders-in-different-scenarios).
- Do not use shell scripts with special permissions, such as SetUID or SetGID.
- Do not use executable files with high-risk capabilities.
- Delete all files without owners.

## Build Security Statement

- In this project, you need to build packages. During the process, some intermediate files and compilation directories are generated. You are advised to perform permission control on these files. You can modify build scripts as needed to avoid security risks and ensure the security of the build results.
- The Python `.whl` package is required. To prevent code tampering and unauthorized changes, you are advised to limit Python to be accessible and modifiable only by the installation user.
- Use the built-in Address Space Layout Randomization (ASLR) and Kernel Address Space Layout Randomization (KASLR) mechanisms of Linux for security.
    - ASLR can be enabled to enhance the protection against vulnerability attacks. The method of enabling ASLR is as follows:
        
        ```shell
        echo 2 > /proc/sys/kernel/randomize_va_space
        ```

    - KASLR can be enabled to increase the difficulty of exploiting kernel vulnerabilities. The method of enabling KASLR is as follows:
    1. View the kernel configuration file.

        ```shell
        vi /boot/config-$(uname -r)
        ```

        If the following information exists, KASLR is supported:

        ```shell
        CONFIG_RANDOMIZE_BASE=y
        ```

    2. Open the `/etc/default/grub` configuration file and add the `kaslr` parameter to the line where `GRUB_CMDLINE_LINUX_DEFAULT` is located. The following is an example:

        ```shell
        GRUB_CMDLINE_LINUX_DEFAULT="kaslr"
        ```

    3. Update the GRUB configuration.

        ```shell
        sudo update-grub
        ```

    4. Restart the system to enable the KASLR function.

        ```shell
        sudo reboot
        ```

- To prevent buffer overflow attacks, you are advised to use the address space layout randomization (ASLR) technology to randomize the layout of linear areas such as the heap, stack, and shared library mapping to make it more difficult for attackers to predict target addresses and locate code. This technology can be applied to heaps, stacks, and memory mapping areas (mmap base addresses, shared libraries, and vDSO pages).
    1. Ensure that the current user has the write permission on the `/proc/sys/kernel/randomize_va_space` file.
    2. Prevent buffer overflow.

        ```shell
        echo 2 >/proc/sys/kernel/randomize_va_space
        ```

## Data Security Statement

- This project involves receiving input, loading model weights, and saving result data. Some APIs directly or indirectly use the risky module pickle, which may pose data risks. Ensure that the input data source and save path are trusted. When loading model weights, you are advised to use local weights.

## Runtime Security Statement

- To prevent information leak during the communication between the service and client, you are advised to enable HTTPS communication and two-way authentication. If they are enabled, you are advised to perform security access control on the certificates, private keys, and passwords involved in communication authentication.
- PyMotor provides only part of flow control capabilities, which do not apply to the public network. You need to guarantee PyMotor flow control and isolation between the public network and LAN. If the open-source software Nginx can be used for assurance, you can deploy Nginx by referring to the [Nginx official documentation](https://nginx.org/en/docs/) and [Ascend community server security hardening](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0068.html).
- You are advised to disable network-wide listening ports and other ports unless necessary.
- You are advised to disable insecure services, such as Telnet and FTP.
- You can limit the rate of connections to the server based on IP addresses to defend against DoS attacks, for example, by enabling the Linux `iptables` firewall or optimizing the `sysctl` parameter.
- By default, the Gloo, DataDist, and HCCL communication in this project do not support TLS authentication. If necessary, see [B-Collective Communication Hardening](#b-collective-communication-hardening).

## Public API Statement

All external APIs provided in this project have been disclosed in the documentation. You are advised to use the documented public APIs. Avoid calling undocumented internal functions.

## Communication Matrix

The communication matrix of this project, including the open ports, transport layer protocols used by the ports, names of the network elements that communicate with the peer end through the ports, authentication modes, and functions, has been disclosed in the documentation. For details, see [MindIE Communication Matrix](https://www.hiascend.com/document/detail/en/mindie/22RC1/ref/commumatrix/Communication0000.html). The latest version in the community prevails.

## Public IP Address Statement

The public network addresses contained in the code of this project have been disclosed in the documentation. For details, see [MindIE Public Network URLs](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0089.html). The latest version in the community prevails.

## Vulnerability Handling Mechanism

[Vulnerability Management](https://gitcode.com/Ascend/community/blob/master/docs/security.md)

## Disclaimer

- This project is intended solely for debugging and development. You are responsible for any risks and should carefully review the following information:

  - [X] Data processing and deletion: Users are responsible for managing and deleting any data (including but not limited to inference results and logs) generated while using this project. You are advised to delete such data promptly after use to prevent information leakage.
  - [X] Data confidentiality and transmission: Users understand and agree not to share or transmit any data generated by this project. Neither this project nor its developers are responsible for any information leak, data breaches, or other negative consequences.
  - [X] User input security: Users are responsible for the security of any commands, parameters, and configuration files they enter and for any security risks or losses resulting from improper input. The project and its developers are not liable for any issues caused by incorrect input.

- Disclaimer scope: This disclaimer applies to all individuals and entities using this project. By using this project, you acknowledge and accept the terms of this statement and agree to assume all risks and responsibilities arising from its use. If you do not agree, please stop using this project immediately.
- Before using this project, **please read and understand the preceding disclaimer**. If you have any questions about this project, contact the developer.

## Appendixes

### A-Recommended Maximum Permissions for Files and Folders in Different Scenarios

| Type          | Maximum Linux Permission|
| -------------- | ---------------  |
| User's home directory                       |   750 (rwxr-x---)           |
| Program files (including scripts and libraries)      |   550 (r-xr-x---)            |
| Program file directory                     |   550 (r-xr-x---)           |
| Configuration files                         |  640 (rw-r-----)            |
| Configuration file directory                     |   750 (rwxr-x---)           |
| Log files (recorded or archived)       |  440 (r--r-----)            |
| Log files (being recorded)               |    640 (rw-r-----)          |
| Log file directory                     |   750 (rwxr-x---)           |
| Debug files                        |  640 (rw-r-----)        |
| Debug file directory                    |   750 (rwxr-x---) |
| Temporary file directory                     |   750 (rwxr-x---)  |
| Maintenance and upgrade file directory                 |   770 (rwxrwx---)   |
| Service data files                     |   640 (rw-r-----)   |
| Service data file directory                 |   750 (rwxr-x---)     |
| Key components, private keys, certificates, and ciphertext file directory   |  700 (rwx------)     |
| Key components, private keys, certificates, and ciphertext files       | 600 (rw-------)     |
| APIs and scripts for encryption and decryption           |   500 (r-x------)       |

### B-Collective Communication Hardening

To compile and install PyTorch that supports TLS, perform the following steps:

- Step 1 Compile PyTorch.
    1. Compile the PyTorch source code.
        
        ```shell
        git clone https://github.com/pytorch/pytorch.git --depth=1 -b v2.1.0
        git submodule sync && git submodule update --init --depth=1 --recursive
        ```  

    2. Install OpenSSL-1.1.
       
        ```shell
        wget https://www.openssl.org/source/openssl-1.1.1w.tar.gz
        tar -xzf openssl-1.1.1w.tar.gz
        cd openssl-1.1.1w
        ./config --prefix=/usr/local/openssl-1.1
        make -j$(nproc)
        sudo make install
        ```  

    3. Export environment variables.
        
        ```shell
        export OPENSSL_ROOT_DIR=/usr/local/openssl-1.1
        export LD_LIBRARY_PATH=$OPENSSL_ROOT_DIR/lib:$LD_LIBRARY_PATH
        export USE_GLOO=1
        export USE_GLOO_WITH_OPENSSL=1
        ```  

    4. Build a Python package.
        
        ```shell
        python3 setup.py bdist_wheel
        ```  

- Step 2 Install PyTorch. To support TLS, install torch 2.1.0a0+git7bcf7da.
    
    ```shell
    cd dist
    pip install --ignore-installed torch-2.1.0a0+git7bcf7da-cp311-cp311-linux_aarch.whl
    ```  

- Step 3 Compile and install Gloo.
    
    ```shell
    git clone https://github.com/pytorch/gloo.git
    mkdir build && cd build
    cmake .. -USE_TCP_OPENSSL_LOAD=ON
    make -j&(nproc)
    sudo make install
    export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH
    ```  

- Step 4 Enable Gloo TLS.
    
    ```shell
    export GLOO_DEVICE_TRANSPORT=TCP_TLS
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_PKEY=/path/to/tls_ca/server.key.pem
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_CERT=/path/to/tls_ca/server.pem
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_CA_FILE=/path/to/tls_ca/ca.pem
    ```  
