# Security Declaration

## Security Precautions

When using MindIE Motor, to ensure security, users should review the network security hardening measures of the entire system based on their own business and perform relevant configurations in accordance with the security policies of their organization, including but not limited to software versions, password complexity requirements, security configurations (protocols, cipher suites, key lengths, etc.), permission configurations, and firewall settings. For more security declarations and recommendations, refer to [MindIE Security Management and Hardening on Ascend Community](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0041.html), subject to the latest version on the community.

## Recommended Operating Environment

- To reduce potential security risks, it is recommended that system operations be performed using non-root, non-administrator accounts, ensuring that only root is the highest-privilege user of the system, that the UIDs of all system accounts are distinct, and that the principle of least privilege is followed.

- Perform regular antivirus scans on the cluster. Routine antivirus checks help protect the cluster from viruses, malicious code, spyware, and program intrusions, reducing risks such as system crashes and information leakage. Mainstream industry antivirus software can be used for antivirus checks.

- To ensure the security of the production environment and reduce the risk of attacks, regularly check [MindIE Security Management and Hardening on Ascend Community](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0041.html) to fix vulnerabilities and functional issues.

## File Permission Control

- It is recommended that users set the umask to 0027 or higher on both the host (including the host machine) and in containers to improve security.

- It is recommended that users properly control access permissions for files containing sensitive content, such as personal privacy data, commercial assets, and business development-related files. For example, for the installation directory permission control and data file permission control in this project, refer to [A-Recommended Maximum Permission Control Values for Files (Folders) in Each Scenario](#a-recommended-maximum-permission-control-values-for-files-folders-in-each-scenario) for the permissions to be set.

- The use of shell scripts with special permissions such as SetUID or SetGID is prohibited.

- The use of executable files with high-risk capabilities is prohibited.

- Files without an owner are not allowed in the system.

## Build Security Statement

- This project requires self-compilation and building to produce the package. The compilation process generates some intermediate files and build directories. It is recommended that users apply proper permission control to these files, modify the build scripts as needed during the build process to avoid related security risks, and pay attention to the security of the build results.

- This project involves the installation of Python whl packages. To avoid risks such as code tampering and forgery caused by other users directly accessing and modifying the Python code, it is recommended that users configure Python so that it can be modified and used only by the installing user.

- Use the Address Space Layout Randomization (ASLR) and Kernel Address Space Layout Randomization (KASLR) mechanisms built into Linux for secure compilation.

    - ASLR, when enabled, enhances protection against vulnerability attacks. It is enabled as follows:

        ```shell
        echo 2 > /proc/sys/kernel/randomize_va_space
        ```

    - KASLR, when enabled, increases the difficulty of attacks targeting kernel vulnerabilities. It is enabled as follows:

    1. Use the following sample command to view the kernel configuration file.

        ```shell
        vi /boot/config-$(uname -r)
        ```

        If the following line exists, KASLR is supported.

        ```shell
        CONFIG_RANDOMIZE_BASE=y
        ```

    2. Open the configuration file `/etc/default/grub` and add the `kaslr` parameter to the line where `GRUB_CMDLINE_LINUX_DEFAULT` is located, as shown in the following example.

        ```shell
        GRUB_CMDLINE_LINUX_DEFAULT="kaslr"
        ```

    3. Use the following command to update the grub configuration.

        ```shell
        sudo update-grub
        ```

    4. Restart the system using the following command to enable the KASLR feature.

        ```shell
        sudo reboot
        ```

- To prevent buffer overflow attacks, it is recommended to use ASLR technology. By randomizing the layout of linear regions such as the heap, stack, and shared library mappings, ASLR increases the difficulty for attackers to predict target addresses and prevents attackers from directly locating attack code. This technology applies to the heap, stack, and memory mapping regions (mmap base address, shared libraries, and vdso page).

    1. Ensure that the current user has write permission on the `/proc/sys/kernel/randomize_va_space` file.

    2. Enable buffer overflow security protection.

        ```shell
        echo 2 >/proc/sys/kernel/randomize_va_space
        ```

## Data Security Declaration

- This project involves receiving input, loading model weights, and saving result data. Some interfaces directly or indirectly use the risky module pickle, which may pose data risks. Ensure that the input data source and the save path are trusted. When loading model weights, it is recommended to use local weights.

## Runtime Security Statement

- To prevent information leakage during communication between the service and clients, users are advised to enable HTTPS communication and mutual authentication. If enabled, it is recommended to implement proper security access control for the certificates, private keys, and passwords involved in communication authentication.

- MindIE Motor provides only partial flow control capabilities and does not directly connect to the public network. Users are advised to properly control MindIE Motor flow control and isolate the public network from the LAN. For example, the open-source software Nginx can be used for protection. Users can refer to the [Nginx Official Documentation](https://nginx.org/en/docs/) and [Ascend Community Server Security Hardening](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0068.html) for Nginx deployment.

- For ports listening on all networks and other ports, it is recommended to close them if not necessary.

- Users are advised to disable insecure services such as Telnet and FTP.

- Users can protect the system against DoS attacks by limiting the connection rate to the server based on IP addresses according to their own business needs. Methods include but are not limited to using the built-in iptables firewall of the Linux system for prevention and optimizing sysctl parameters.

- The default Gloo, DataDist, and HCCL communication in this project does not currently support TLS authentication. If needed, refer to [B-Collective Communication Hardening](#b-collective-communication-hardening).

## Public Interface Declaration

All external interfaces provided by this project have been disclosed in the documentation. It is recommended to directly use the public interfaces described in the documentation, and it is not recommended to directly invoke the source code of interfaces that are not explicitly disclosed.

## Communication Matrix

The communication matrix of this project, including the ports opened by the product, the transport layer protocol used by each port, the name of the communication network element that communicates with the peer through the port, the authentication method, the purpose, and other information, has been disclosed in the documentation. For details, see [MindIE Communication Matrix on Ascend Community](https://www.hiascend.com/document/detail/en/mindie/22RC1/ref/commumatrix/Communication0000.html). For details, see the latest upstream version.

## Public Network Address Declaration

All public network address declarations contained in the code of this project have been disclosed in the documentation. For details, see [MindIE Public Network URLs on Ascend Community](https://www.hiascend.com/document/detail/en/mindie/22RC1/envdeployment/instg/mindie_instg_0089.html). The latest community version prevails.

## Vulnerability Mechanism Description

[Vulnerability Management](https://gitcode.com/Ascend/community/blob/master/docs/security.md)

## Disclaimer

- This project is intended solely for debugging and development purposes. Users assume all risks associated with its use and acknowledge the following:

  - [X] Data processing and deletion: Data generated by users during the use of this project (including but not limited to inference results and logs) falls within the scope of user responsibility. Users are advised to promptly delete relevant data after use to prevent leakage or unnecessary information disclosure.

  - [X] Data confidentiality and dissemination: Users understand and agree that data generated through this project must not be arbitrarily distributed or disseminated. This project and its developers shall not be held liable for any information leakage, data leakage, or other adverse consequences arising therefrom.

  - [X] User input security: Users are responsible for ensuring the security of the command lines, parameters, and configuration files they input, and shall bear any security risks or losses arising from improper input. This project and its developers shall not be held liable for any issues caused by improper input.

- Scope of the disclaimer: This disclaimer applies to all individuals or entities using this project. By using this project, you agree to and accept the contents of this statement and are willing to assume the risks and responsibilities arising from the use of this functionality. If you have any objections, please discontinue use of this project.

- Before using this project, please **carefully read and understand the contents of the above disclaimer**. For any issues or questions arising from the use of this project, please contact the developers in a timely manner.

## Appendix

### A-Recommended Maximum Permission Control Values for Files (Folders) in Each Scenario

| Type           | Linux Permission Reference Maximum Value |
| -------------- | ---------------  |
| User home directory                        |   750 (rwxr-x---)            |
| Program files (including script files, library files, etc.)       |   550 (r-xr-x---)             |
| Program file directory                      |   550 (r-xr-x---)            |
| Configuration file                          |  640 (rw-r-----)             |
| Configuration file directory                      |   750 (rwxr-x---)            |
| Log file (recording completed or archived)        |  440 (r--r-----)             |
| Log file (being recorded)                |    640 (rw-r-----)           |
| Log file directory                      |   750 (rwxr-x---)            |
| Debug file                         |  640 (rw-r-----)         |
| Debug file directory                     |   750 (rwxr-x---)  |
| Temporary file directory                      |   750 (rwxr-x---)   |
| Maintenance and upgrade file directory                  |   770 (rwxrwx---)    |
| Service data file                      |   640 (rw-r-----)    |
| Service data file directory                  |   750 (rwxr-x---)      |
| Directory of key components, private keys, certificates, and ciphertext files    |  700 (rwx-----)      |
| Key components, private keys, certificates, and encrypted ciphertext        | 600 (rw-------)      |
| Encryption/decryption interfaces and encryption/decryption scripts            |   500 (r-x------)        |

### B-Collective Communication Hardening

The steps for compiling and installing PyTorch with TLS support are as follows.

- Step 1 Compile PyTorch.

    1. Compile the PyTorch source code.

        ```shell
        git clone https://github.com/pytorch/pytorch.git --depth=1 -b v2.1.0
        git submodule sync && git submodule update --init --depth=1 --recursive
        ```

    2. Install openssl-1.1.

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

    4. Build the Python package.

        ```shell
        python3 setup.py bdist_wheel
        ```

- Step 2 Install PyTorch. TLS support requires installing torch 2.1.0a0+git7bcf7da.

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

- Step 4 Enable GLOO TLS.

    ```shell
    export GLOO_DEVICE_TRANSPORT=TCP_TLS
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_PKEY=/path/to/tls_ca/server.key.pem
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_CERT=/path/to/tls_ca/server.pem
    export GLOO_DEVICE_TRANSPORT_TCP_TLS_CA_FILE=/path/to/tls_ca/ca.pem
    ```
