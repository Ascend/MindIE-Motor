# Active/Standby Switchover Feature

The active/standby switchover feature is implemented primarily through ETCD distributed locks to ensure high availability of the system. It includes active/standby Controller and active/standby Coordinator.

## Controller Active/Standby Switchover

### Feature Description

This feature implements the active/standby switchover of the Controller in a Kubernetes cluster through the ETCD distributed lock mechanism, ensuring high availability of the system. After the Controller active/standby switchover feature switch is enabled, the system starts two Controller instances during the initialization phase and performs active/standby identity election through ETCD distributed lock contention. When the active Controller fails, the standby Controller automatically takes over after a configured time interval.

**Restrictions and Constraints**

- It is not recommended to deploy the active and standby Controller nodes on the same node.

- The ETCD server side must use v3.6.

- The feature takes effect only when the ETCD server side is correctly deployed. The server side requires at least three replicas to ensure the reliability of the ETCD cluster.

- The Coordinator and Controller active/standby switchover features can share one ETCD. Multiple MoE EP clusters can share one ETCD, distinguished by namespace.

### Deployment Process

#### Generating the ETCD Security Certificate (Optional for Controller Active/Standby Switchover)

The Controller active/standby switchover depends on the ETCD distributed lock feature, which involves communication between different PODs in the cluster. It is recommended to use CA certificates for mutual authentication. For certificate configuration, see [Generating the ETCD Security Certificate](#generating-etcd-security-certificates-optional-for-etcd-cluster-deployment).
>[!NOTE]NOTE
>If CA certificates are not used for mutual authentication and encrypted communication, services will transmit data in plaintext, which may pose a high network security risk.

#### Deploying the ETCD Server

Only one set of ETCD server side needs to be deployed. For details, see [ETCD Deployment](#deploying-the-etcd-server).

#### (Optional) Configuration on the K8s Management Side

When a hardware fault occurs (for example, a machine restart), the K8s cluster cannot promptly detect the status of container Pods, causing the inference service to fail to recover within the specified time. You can perform the following steps to accelerate service recovery.

>[!NOTE]NOTE
>If the impact duration of hardware faults is not a concern, you can skip the following steps.

1. Run the following command to query the node heartbeat timeout marking threshold (`node-monitor-grace-period`) of the K8s management node. If the result is empty, the default value is used.

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

2. Run the following command to open and modify the node heartbeat timeout marking threshold (`node-monitor-grace-period`). The configuration file is generally stored in the `/etc/kubernetes/manifests/kube-controller-manager.yaml` directory on the control plane node (the node running `kube-controller-manager`).

    ```bash
    vi /etc/kubernetes/manifests/kube-controller-manager.yaml
    ```

      The modifications are as follows:

      ```yaml
        apiVersion: v1
        kind: Pod
        metadata:
        name: kube-controller-manager-<control plane node name>  # For example, kube-controller-manager-node-97-10
        namespace: kube-system
        spec:
        containers:
        - command:
            - kube-controller-manager
            # Other original parameters... (keep unchanged)
            - --kubeconfig=/etc/kubernetes/controller-manager.conf
            - --authentication-kubeconfig=/etc/kubernetes/controller-manager.conf
            # Add/modify the node-monitor-grace-period parameter (change it to 20s)
            - --node-monitor-grace-period=20s
            # Other original parameters...
      ```

3. Press `Esc`, type `:wq!`, and press `Enter` to save and exit editing.

4. Run the following command to restart the K8s service on the node where `kube-controller-manager` resides, thereby restarting the `kube-controller-manager` service.

    ```bash
    systemctl restart kubelet.service
    ```

5. Run the following command to verify whether the parameter takes effect:

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

    The following output indicates that the parameter has taken effect:

    ```bash
    --node-monitor-grace-period=20s
    ```

#### Configuring MindIE Motor

1. Configure certificate mounting on the Controller side. (**If CA certificate is not enabled, skip this step.**)

    If CA certificate authentication needs to be enabled, mount the generation path of the certificate files to the Controller container based on the certificate files generated in [Generating the ETCD Security Certificate](#generating-etcd-security-certificates-optional-for-etcd-cluster-deployment). Add the following content to `volumeMounts` and `volumes` in the `deployment/controller_init.yaml` file (`controller-ca` is the mounted certificate directory):

    ```yaml
    ...
          volumeMounts:
          ...
          - name: controller-ca
            mountPath: /usr/local/Ascend/pyMotor/conf/security/etcd # Mount path of the physical machine /home/{username}/auto_gen_ms_cert directory in the container
      volumes:
      ...
      - name: controller-ca
        hostPath:
          path: /home/{username}/auto_gen_ms_cert # Path for creating files and generated files on the physical machine
          type: Directory
    ...
    ```

2. Configure the `user_config.json` configuration file to enable TLS authentication. (**If CA certificate is not enabled, skip this step.**)

    Enable CA certificate authentication:

    - Set `enable_tls` in `tls_config`/`etcd_tls_config` to `true`;

    - Set `ca_file`/`cert_file`/`key_file`/`passwd_file`/`tls_crl` to the corresponding file paths.

    ```json
    ...
      "tls_config": {
        ...
        "etcd_tls_config": {
          "enable_tls": true,
          "ca_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/ca.pem",
          "cert_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.pem",
          "key_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.key",
          "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/key_pwd.txt",
          "tls_crl": ""
        },
        ...
      }
   ...
   ```

3. Enable the Controller active/standby switchover feature in the `user_config.json` configuration file. The configuration parameters are as follows. Change `enable_master_standby` to `true`.

    ```json
    ...
       "motor_controller_config": {
          "standby_config": {
             "enable_master_standby": true
          }
       }
    ...
    ```

    - `false`: disables the active/standby switchover;

    - `true`: enables the active/standby switchover.

    >[!NOTE]NOTE
    > By default, the ETCD server side in the default namespace is used, and the default port number is 2379. To modify it, change the ETCD information in `motor_controller_config`. The domain name is usually `etcd.{namespace}.svc.cluster.local`.
    >
    >```json
    > ...
    >   "motor_controller_config": {
    >      "standby_config": {
    >         "enable_master_standby": true
    >      },
    >      "etcd_config": {
    >         "etcd_host": "etcd.default.svc.cluster.local",
    >         "etcd_port": 2379
    >      }
    >   }
    > ...
    > ```

#### Starting vLLM

1. Run the following command in the `examples/deployer` directory to start. You can specify a configuration directory or specify a configuration file separately.

    ```bash
    cd examples/deployer
    # (Recommended) Method 1: Specify a configuration directory
    python deploy.py --config_dir ../infer_engines/vllm

    # Method 2: Specify a configuration file separately
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    >[!NOTE]NOTE
    > - You can determine the Controller active/standby nodes by querying the logs of the corresponding nodes. If "Role changed from standby to master" appears in the log, it indicates that the current node has acquired the ETCD distributed lock and is the active node.<br>
    > - You can run the K8s command `kubectl get pod -A -owide` to view the pod list. If exactly one Controller pod has a `READY` status of `1/1`, it indicates that this node is the active Controller node.

2. Send a request to verify whether the service has started successfully.

    There are two ways to send a request:

    - Virtual IP and port number of the active Controller node: `http://PodIP:1025`. (Only the pod with `READY` status `1/1` can execute inference requests.)

    - Any physical machine IP in the K8s cluster: 31015 (the port number must be consistent with the `nodePort` of `mindie-motor-coordinator-infer` in `examples/deployer/yaml_template/coordinator_template.yaml` (multi_deployment scenario) or `examples/deployer/yaml_template/infer_service_template.yaml` (CRD scenario)).

    Example of using the physical machine IP and port number:

    ```bash
    #!/bin/bash
    url="http://{physical machine IP address}:31015/v1/chat/completions"
    data='{
        "model": "deepseek",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "Who are you"}]
    }'
    curl  $url -X POST  -d "$data"
    ```

    The following output indicates that the service starts successfully:

    ```txt
    ...
       "message": {
          "role": "assistant",
          "content": "<think>\nOK, the user asks who I am. I",
       ...
       }
    ...
    ```

## Coordinator Active/Standby Switchover

### Feature Description

This feature implements the active/standby switchover of the Coordinator in a Kubernetes cluster through the ETCD distributed lock mechanism to ensure high availability of the system. When the Coordinator active/standby switchover feature switch is enabled, two Coordinators are started during initialization, and the active/standby identity is determined through ETCD distributed lock competition. When the active Coordinator fails, the standby Coordinator automatically takes over the work after a certain time interval.

**Restrictions and Constraints**

- It is not recommended to deploy the active and standby Coordinator nodes on the same node.

- The ETCD server side must use version v3.6.

- The feature taking effect depends on the correct deployment of the ETCD server side. The server side requires at least three replicas to ensure the reliability of the ETCD cluster.

- The Coordinator and Controller active/standby switchover features can share one set of ETCD. Multiple MoE-EP clusters can share one set of ETCD, distinguished by namespace.

### Deployment Process

#### Generating ETCD Security Certificate (Optional for Coordinator Active/Standby Switchover)

The Coordinator active/standby switchover depends on the ETCD distributed lock feature, which involves communication between different PODs in the cluster. It is recommended to use CA certificates for mutual authentication. For certificate configuration, see [Certificate Generation](#generating-etcd-security-certificates-optional-for-etcd-cluster-deployment).
<b>If CA certificates are not enabled, skip this step.</b>

#### Deploying the ETCD Server

The active/standby switchover feature of Coordinator and Controller can share one set of ETCD; multiple MoE EP clusters can share one set of ETCD, distinguished by namespace.

#### (Optional) Configuration on the Kubernetes Management Side

When a hardware fault occurs (for example, a machine restart), the Kubernetes cluster cannot promptly detect the status of container Pods, causing the inference service to fail to recover within the specified time. You can perform the following steps to accelerate service recovery.

>[!NOTE]NOTE
>If the duration affected by hardware faults is not a concern, you can skip the following steps.

1. Run the following command to query the `node-monitor-grace-period` threshold of the Kubernetes management node. If the result is empty, the default value is used.

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

2. Run the following command to open and modify the `node-monitor-grace-period` threshold. The configuration file is generally stored in the `/etc/kubernetes/manifests/kube-controller-manager.yaml` directory on the control plane node (the node running `kube-controller-manager`).

    ```bash
    vi /etc/kubernetes/manifests/kube-controller-manager.yaml
    ```

      The modifications are as follows:

      ```yaml
        apiVersion: v1
        kind: Pod
        metadata:
        name: kube-controller-manager-<control plane node name>  # For example, kube-controller-manager-node-97-10
        namespace: kube-system
        spec:
        containers:
        - command:
            - kube-controller-manager
            # Other original parameters... (keep unchanged)
            - --kubeconfig=/etc/kubernetes/controller-manager.conf
            - --authentication-kubeconfig=/etc/kubernetes/controller-manager.conf
            # Add/modify the node-monitor-grace-period parameter (change to 20s)
            - --node-monitor-grace-period=20s
            # Other original parameters...
      ```

3. Press `Esc`, type `:wq!`, and press `Enter` to save and exit editing.

4. Run the following command to restart the K8s service on the node where `kube-controller-manager` resides, thereby restarting the `kube-controller-manager` service.

    ```bash
    systemctl restart kubelet.service
    ```

5. Run the following command to verify whether the parameters have taken effect:

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

    The following output indicates that the parameters have taken effect:

    ```txt
    --node-monitor-grace-period=20s
    ```

#### Configuring MindIE Motor

1. (Optional) Configure certificate mounting on the Coordinator side. (**If CA certificate authentication is not enabled, skip this step.**)

    If CA certificate authentication needs to be enabled, mount the generation path of the certificate files generated according to [Generating ETCD Security Certificate](#generating-etcd-security-certificates-optional-for-etcd-cluster-deployment) into the Coordinator container. Add the following content to `volumeMounts` and `volumes` in the `examples/deployer/yaml_template/coordinator_template.yaml` file (`coordinator-ca` is the mounted certificate directory):

    ```yaml
    ...
          volumeMounts:
          - name: motor-config
            mountPath: /mnt/configmap
          - name: coredump
            mountPath: /var/coredump
          - name: mnt
            mountPath: /mnt
          - name: coordinator-ca
            mountPath: /usr/local/Ascend/pyMotor/conf/security/etcd # Mount path of the physical machine /home/{username}/auto_gen_ms_cert directory in the container
      volumes:
      - name: motor-config
        configMap:
          name: motor-config
          defaultMode: 0550
      - name: coredump
        hostPath:
          path: /var/coredump
          type: DirectoryOrCreate
      - name: mnt
        hostPath:
          path: /mnt
      - name: coordinator-ca
        hostPath:
          path: /home/{username}/auto_gen_ms_cert # Path for creating and generating files on the physical machine
          type: Directory
    ...
    ```

2. (Optional) Configure the `user_config.json` configuration file to enable TLS authentication. (**If CA certificate authentication is not enabled, skip this step.**)

    Enable CA certificate authentication:

    - Set `enable_tls` of `tls_config`/`etcd_tls_config` to `true`;

    - Set `ca_file`/`cert_file`/`key_file`/`passwd_file`/`tls_crl` to the corresponding file paths.

    ```json
    ...
      "tls_config": {
        ...
        "etcd_tls_config": {
          "enable_tls": true,
          "ca_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/ca.pem",
          "cert_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.pem",
          "key_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.key",
          "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/key_pwd.txt",
          "tls_crl": ""
        },
        ...
      }
   ...
   ```

3. Enable the Coordinator active/standby switchover feature in the `user_config.json` configuration file. The configuration parameters are as follows. Change `enable_master_standby` to `true`.

    ```json
    ...
       "motor_coordinator_config": {
          "standby_config": {
             "enable_master_standby": true
          }
       }
    ...
    ```

    - `false`: disables the active/standby switchover;

    - `true`: enables the active/standby switchover.

    >[!NOTE]NOTE
    > By default, the ETCD server side in the default namespace is used, and the port number defaults to 2379. If you need to modify it, change the ETCD information in `motor_coordinator_config`. The domain name is usually `etcd.{namespace}.svc.cluster.local`.
    >
    > ```json
    > ...
    >    "motor_coordinator_config": {
    >       "standby_config": {
    >          "enable_master_standby": true
    >       },
    >       "etcd_config": {
    >          "etcd_host": "etcd.default.svc.cluster.local",
    >          "etcd_port": 2379
    >       }
    >    }
    > ...
    > ```

#### Starting vLLM

1. Run the following command in the `examples/deployer` directory to start the service. You can specify a configuration directory or specify a configuration file separately.

    ```bash
    cd examples/deployer
    # (Recommended) Method 1: Specify a configuration directory 
    python deploy.py --config_dir ../infer_engines/vllm

    # Method 2: Specify a configuration file separately
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    >[!NOTE]NOTE
    > - You can determine the Coordinator active and standby nodes by querying the logs of the corresponding nodes. If "Role changed from standby to master" appears in the log, it indicates that the current node has acquired the ETCD distributed lock and is the active node.
    > - You can run the K8s command "kubectl get pod -A -owide" to view the pod list. If exactly one Coordinator pod has a `READY` status of `1/1`, this node is the Coordinator active node.

2. Send a request to verify that the service has started successfully.

    There are two ways to send a request:

    - Virtual IP address and port number of the active Coordinator node: `http://PodIP:1025`. (Only the pod whose `READY` status is `1/1` can execute inference requests.)

    - Any physical machine IP in the K8s cluster: `31015` (the port number must be consistent with the `nodePort` of `mindie-motor-coordinator-infer` in `examples/deployer/yaml_template/coordinator_template.yaml` (multi_deployment scenario) or `examples/deployer/yaml_template/infer_service_template.yaml` (CRD scenario)).

    This sample uses the physical machine IP and port number method.

    ```json
    #!/bin/bash
    url="http://{physical machine IP address}:31015/v1/chat/completions"
    data='{
        "model": "deepseek",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "Who are you"}]
    }'
    curl  $url -X POST  -d "$data"
    ```

    The following output indicates that the service started successfully:

    ```json
    ...
       "message": {
          "role": "assistant",
          "content": "<think>\nOK, the user asks who I am. I",
       ...
       }
    ...
    ```

## ETCD Cluster Deployment

ETCD cluster deployment consists of two parts: generating ETCD security certificates and deploying the ETCD server.

### Generating ETCD Security Certificates (Optional for ETCD Cluster Deployment)

>[!NOTE]NOTE
>If CA certificates are not used for mutual authentication encrypted communication, plaintext transmission will occur between services, which may pose a high network security risk.

1. Prepare the prerequisite files for certificate generation. The following example uses `/home/{username}/auto_gen_ms_cert` as the directory for storing the files.

    **`server.cnf`**

    ```txt
    [req] # Main request content
    req_extensions = v3_req
    distinguished_name = req_distinguished_name

    [req_distinguished_name] # Certificate subject information
    countryName = CN
    stateOrProvinceName = State
    localityName = City
    organizationName = Organization
    organizationalUnitName = Unit
    commonName = etcd-server

    [v3_req] # Core attributes
    basicConstraints = CA:FALSE
    keyUsage = digitalSignature, keyEncipherment
    extendedKeyUsage = serverAuth, clientAuth
    subjectAltName = @alt_names

    [alt_names] # Service identifier
    DNS.1 = etcd
    DNS.2 = etcd.default
    DNS.3 = etcd.default.svc
    DNS.4 = etcd.default.svc.cluster.local  #ETCD must be deployed in the default namespace
    DNS.5 = etcd-0.etcd
    DNS.6 = etcd-0.etcd.default.svc.cluster.local
    DNS.7 = etcd-1.etcd
    DNS.8 = etcd-1.etcd.default.svc.cluster.local
    DNS.9 = etcd-2.etcd
    DNS.10 = etcd-2.etcd.default.svc.cluster.local
    ```

    **`client.cnf`**

    ```txt
    [req] # Main request content
    req_extensions = v3_req
    distinguished_name = req_distinguished_name

    [req_distinguished_name] # Certificate subject information
    countryName = CN
    stateOrProvinceName = State
    localityName = City
    organizationName = Organization
    organizationalUnitName = Unit
    commonName = etcd-client

    [v3_req] # Core attributes
    basicConstraints = CA:FALSE
    keyUsage = digitalSignature, keyEncipherment
    extendedKeyUsage = clientAuth
    subjectAltName = @alt_names

    [alt_names] # Service identifier
    DNS.1 = mindie-service-controller
    DNS.2 = mindie-service-coordinator

    ```

    **`crl.conf`**

    ```txt
    # OpenSSL configuration for CRL generation
    #
    ####################################################################
    [ ca ] # CA framework declaration, indicating which predefined CA configuration block OpenSSL uses as the default setting
    default_ca = CA_default # The default ca section
    ####################################################################
    [ CA_default ] # Core CA settings, all critical paths, files, and default operations
    dir             = {dir}  # Add this root directory definition, such as /home/{username}/auto_gen_ms_cert
    database        = $dir/etcd_crl/index.txt
    crlnumber       = $dir/etcd_crl/pulp_crl_number
    new_certs_dir   = $dir/etcd_crl/newcerts
    certificate     = $dir/ca.pem
    private_key     = $dir/ca.key
    serial          = $dir/etcd_crl/serial

    default_days = 365 # how long to certify for
    default_crl_days= 365 # how long before next CRL
    default_md = default # use public key default MD
    preserve = no # keep passed DN ordering
    policy = policy_anything
    ####################################################################
    [ policy_anything ]
    countryName             = optional  # C: optional
    stateOrProvinceName     = optional  # ST: optional
    localityName            = optional  # L (city): optional
    organizationName        = optional  # O: optional
    organizationalUnitName  = optional  # OU: optional
    commonName              = supplied  # CN: required
    emailAddress            = optional  # Email: optional
    ####################################################################
    [ crl_ext ] # CRL extension attributes
    # CRL extensions.
    # Only issuerAltName and authorityKeyIdentifier make any sense in a CRL.
    # issuerAltName=issuer:copy
    authorityKeyIdentifier=keyid:always,issuer:always
    ```

    >[!NOTE]NOTE
    >It is recommended that the `{dir}` path in the file be a shared directory accessible to all nodes.

    **`gen_etcd_controller_ca.sh`**

    ```bash
    #!/bin/bash
    # Configure the base directory, corresponding to crl.conf
    base_dir=/home/{username}/auto_gen_ms_cert
    # 1. Create the required files and directories
    mkdir -p ${base_dir}/etcd_crl/newcerts
    touch ${base_dir}/etcd_crl/index.txt
    echo 1000 > ${base_dir}/etcd_crl/pulp_crl_number
    echo "01" > ${base_dir}/etcd_crl/serial
    # 2. Set permissions
    chmod 700 ${base_dir}/etcd_crl/newcerts
    chmod 600 ${base_dir}/etcd_crl/{index.txt,pulp_crl_number,serial}
    # 3. Create the CA certificate
    openssl genrsa -aes256 -out ca.key 4096
    openssl req -x509 -new -nodes -key ca.key \
    -subj "/CN=my-cluster-ca" \
    -days 3650 -out ca.pem
    # 4. Generate the server certificate
    openssl genrsa -out server.key 4096
    openssl req -new -key server.key -out server.csr \
    -subj "/CN=etcd-server" -config server.cnf
    openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out server.pem -days 3650 -extensions v3_req -extfile server.cnf
    # 5. Generate the client certificate
    openssl genrsa -out client.key 4096
    openssl req -new -key client.key -out client.csr \
    -subj "/CN=inst0-client" -config client.cnf
    openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out client.pem -days 3650 -extensions v3_req -extfile client.cnf
    # 6. Set permissions
    chmod 0400 ./*.key
    chmod 0400 ./*.pem
    ```

    >[!NOTE]NOTE
    > Remember the password used when generating `ca.key`, as it is required when generating new certificates.

2. Run the following command to execute `gen_etcd_controller_ca.sh` and generate files such as the server certificate and client certificate.

    ```bash
    bash gen_etcd_controller_ca.sh
    ```

    A response similar to the following indicates successful generation:

    ```txt
    Enter PEM pass phrase:
    Verifying - Enter PEM pass phrase:
    Enter pass phrase for ca.key:
    Certificate request self-signature ok
    subject=CN = etcd-server
    Enter pass phrase for ca.key:
    Certificate request self-signature ok
    subject=CN = inst0-client
    Enter pass phrase for ca.key:
    ```

    After the execution is complete, the following files or directories are generated in the current directory:

    ```txt
    ca.key
    ca.pem
    ca.srl
    client.cnf
    client.csr
    client.key
    client.pem
    crl.conf
    etcd_crl   # CRL-related folder
    gen_etcd_controller_ca.sh
    server.cnf
    server.csr
    server.key
    server.pem
    ```

    Through the preceding operations, a CA certificate, a server-side certificate, and a client-side certificate are generated. (The CA certificate is used for authentication, the server certificate is used for ETCD cluster deployment, and the client certificate is used for Controller/Coordinator active/standby switchover.)

    >[!NOTE]NOTE
    > If multiple client-side certificates are required, use the same CA certificate and repeat the following operations:
    >
    > ```bash
    > openssl genrsa -out {new client}.key 4096
    > openssl req -new -key {new client}.key -out {new client}.csr \
    > -subj "/CN={CN of the new client}" -config client.cnf
    > openssl x509 -req -in {new client}.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    > -out {new client}.pem -days 3650 -extensions v3_req -extfile client.cnf
    > # Modify the permissions of the newly generated certificates
    > chmod 0400 ./*.key
    > chmod 0400 ./*.pem
    >  ```

### Deploying the ETCD Server

The following is a deployment reference example.

1. Run the following command to load the ETCD image:

    ```bash
    docker pull quay.io/coreos/etcd:v3.6.0-rc.4
    ```

    >[!NOTE]NOTE
    >If `docker pull` fails, you can use the `podman` command to download the ETCD image, save it, and then import it using the `docker load` command. The commands are as follows:
    >
    > ```bash
    > apt install podman
    > podman pull quay.io/coreos/etcd:v3.6.0-rc.4
    > ```
    >
    > ETCD requires at least three replicas. Import this image on the specified nodes.

2. Create ETCD resources in the cluster.

    1. Run the following command to create the `local-pvs.yaml` file.

        ```bash
        vim local-pvs.yaml
        ```

        Write the following content into the file:

        ```yaml
        # local-pvs.yaml creates the PV
        apiVersion: v1
        kind: PersistentVolume
        metadata:
          name: etcd-data-0  # Must match the PVC naming rule of the StatefulSet
        spec:
          capacity:
            storage: 4096M
          volumeMode: Filesystem
          accessModes: [ReadWriteOnce]
          persistentVolumeReclaimPolicy: Retain
          storageClassName: local-storage  # Must match the storageClass of the PVC
          local:
            path: /mnt/data/etcd-0  # Actual path on the node
          nodeAffinity:
            required:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: kubernetes.io/hostname
                      operator: In
                      values: ["ubuntu"]  # Bind to a specific node, that is, NodeName

        ---
        apiVersion: v1
        kind: PersistentVolume
        metadata:
          name: etcd-data-1
        spec:
          capacity:
            storage: 4096M
          accessModes: [ReadWriteOnce]
          persistentVolumeReclaimPolicy: Retain
          storageClassName: local-storage
          local:
            path: /mnt/data/etcd-1
          nodeAffinity:
            required:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: kubernetes.io/hostname
                      operator: In
                      values: ["worker-80-39"] # Bind to a specific node, that is, NodeName

        ---
        apiVersion: v1
        kind: PersistentVolume
        metadata:
          name: etcd-data-2
        spec:
          capacity:
            storage: 4096M
          accessModes: [ReadWriteOnce]
          persistentVolumeReclaimPolicy: Retain
          storageClassName: local-storage
          local:
            path: /mnt/data/etcd-2
          nodeAffinity:
            required:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: kubernetes.io/hostname
                      operator: In
                      values: ["worker-153"] # Bind to a specific node, that is, NodeName
        ```

        The key parameters are described as follows:

        - `spec.local.path`: path of the corresponding node. It must be real and exist.

        - `spec.nodeAffinity.required.nodeSelectorTerms.matchExpressions.values`: name of the node to be deployed.

    2. Run the following command on the master node of the K8s cluster to create PVs.

        ```bash
        kubectl apply -f local-pvs.yaml
        ```

        If the following result is returned, the PVs are created successfully:

        ```bash
        persistentvolume/etcd-data-0 created
        persistentvolume/etcd-data-1 created
        persistentvolume/etcd-data-2 created
        ```

    3. Run the following command to add the `app=etcd` label to the three nodes.

        ```bash
        kubectl label nodes <node name> app=etcd
        ```

        The following result indicates that the creation is successful:

        ```bash
        node/<node name> labeled
        ```

    4. Run the following command to create the `etcd.yaml` file and configure the ETCD Pod-side certificate.

        ```bash
        vim etcd.yaml
        ```

        Based on the certificate generated in [Generatign ETCD Security Certificate](#generating-etcd-security-certificates-optional-for-etcd-cluster-deployment), mount the generated file path into the ETCD container, configure ETCD to use encrypted communication, and specify `ca.pem`, `server.pem`, and `server.key` for communication.

        ```yaml
        # etcd.yaml creates a synchronized ETCD database on 3 nodes
        ---
        apiVersion: v1
        kind: Service
        metadata:
          name: etcd
          namespace: default
        spec:
          type: ClusterIP
          clusterIP: None # Headless Service, used for DNS resolution of the StatefulSet
          selector:
            app: etcd  # Select the Pod with the label app=etcd
          publishNotReadyAddresses: true  # Allow unready Pods to be discovered by DNS
          ports:
            - name: etcd-client
              port: 2379 # Client communication port
            - name: etcd-server
              port: 2380 # Inter-node communication port
            - name: etcd-metrics
              port: 8080 # ETCD cluster management port
        ---
        apiVersion: apps/v1
        kind: StatefulSet
        metadata:
          name: etcd
          namespace: default
        spec:
          serviceName: etcd # Bind the Headless Service
          replicas: 3 # Odd number of nodes to ensure Raft
          podManagementPolicy: OrderedReady # Allow parallel startup (requires an initialization script)
          updateStrategy:
            type: RollingUpdate # Rolling update strategy
          selector:
            matchLabels:
              app: etcd # Match the Pod label
          template:
            metadata:
              labels:
                app: etcd # Pod label
              annotations:
                serviceName: etcd
            spec:
              affinity:
                podAntiAffinity:
                  requiredDuringSchedulingIgnoredDuringExecution:
                    - labelSelector:
                        matchExpressions:
                          - key: app
                            operator: In
                            values: [etcd]
                      topologyKey: "kubernetes.io/hostname" # Deploy across nodes
              containers:
                - name: etcd
                  image: quay.io/coreos/etcd:v3.6.0-rc.4
                  imagePullPolicy: IfNotPresent
                  ports:
                    - name: etcd-client
                      containerPort: 2379
                    - name: etcd-server
                      containerPort: 2380
                    - name: etcd-metrics
                      containerPort: 8080
                  env:
                    - name: K8S_NAMESPACE
                      valueFrom:
                        fieldRef:
                          fieldPath: metadata.namespace
                    - name: HOSTNAME
                      valueFrom:
                        fieldRef:
                          fieldPath: metadata.name
                    - name: SERVICE_NAME
                      valueFrom:
                        fieldRef:
                          fieldPath: metadata.annotations['serviceName']
                    - name: ETCDCTL_ENDPOINTS
                      value: "$(HOSTNAME).$(SERVICE_NAME):2379"
                    - name: URI_SCHEME
                      value: "https"
                  command:
                    - /usr/local/bin/etcd
                  args:
                    - --log-level=debug
                    - --name=$(HOSTNAME) # Unique node identifier
                    - --data-dir=/data # Data storage path
                    - --wal-dir=/data/wal
                    - --listen-peer-urls=https://0.0.0.0:2380 # Listen for inter-node communication
                    - --listen-client-urls=https://0.0.0.0:2379 # Listen for client requests
                    - --advertise-client-urls=https://$(HOSTNAME).$(SERVICE_NAME):2379  # Client address
                    - --initial-cluster-state=new # New cluster initialization mode
                    - --initial-cluster-token=etcd-$(K8S_NAMESPACE) # Unique cluster identifier
                    - --initial-cluster=etcd-0=https://etcd-0.etcd:2380,etcd-1=https://etcd-1.etcd:2380,etcd-2=https://etcd-2.etcd:2380 # Initial node list
                    - --initial-advertise-peer-urls=https://$(HOSTNAME).$(SERVICE_NAME):2380 # Advertised inter-node communication address
                    - --listen-metrics-urls=http://0.0.0.0:8080 # Cluster management address
                    - --quota-backend-bytes=8589934592
                    - --auto-compaction-retention=5m
                    - --auto-compaction-mode=revision
                    - --client-cert-auth
                    - --cert-file=/etc/ssl/certs/etcdca/server.pem
                    - --key-file=/etc/ssl/certs/etcdca/server.key
                    - --trusted-ca-file=/etc/ssl/certs/etcdca/ca.pem
                    - --peer-client-cert-auth
                    - --peer-trusted-ca-file=/etc/ssl/certs/etcdca/ca.pem
                    - --peer-cert-file=/etc/ssl/certs/etcdca/server.pem
                    - --peer-key-file=/etc/ssl/certs/etcdca/server.key
                  volumeMounts:
                    - name: etcd-data
                      mountPath: /data # Mount persistent storage
                    - name: etcd-ca
                      mountPath: /etc/ssl/certs/etcdca # Mount path of the /home/{username}/auto_gen_ms_cert directory on the physical machine in the container
              volumes:
                - name: crt
                  hostPath:
                    path: /usr/local/Ascend/driver
                - name: etcd-ca
                  hostPath:
                    path: /home/{username}/auto_gen_ms_cert # Path for creating and generating files on the physical machine
                    type: Directory
          volumeClaimTemplates:
            - metadata:
                name: etcd-data
              spec:
                accessModes: [ "ReadWriteOnce" ] # Single-node read/write
                storageClassName: local-storage
                resources:
                  requests:
                    storage: 4096M # Storage space
        ```

        The key parameters are as follows:

        - `spec.template.spec.containers.args.--client-cert-auth`: Enable client certificate authentication

        - spec.template.spec.containers.args.--cert-file: Specifies the server certificate.

        - spec.template.spec.containers.args.--key-file: Specifies the server private key.

        - spec.template.spec.containers.args.--trusted-ca-file: Specifies the trusted CA root certificate.

        - spec.template.spec.containers.args.--peer-client-cert-auth: Enables client certificate authentication between peer nodes.

        - spec.template.spec.containers.args.--peer-trusted-ca-file: Specifies the trusted CA root certificate (for peers).

        - spec.template.spec.containers.args.--peer-cert-file: Specifies the certificate of this node as a peer.

        - spec.template.spec.containers.args.--peer-key-file: Specifies the private key of this node as a peer.

    5. Run the following command on the master node of the K8s cluster to deploy the ETCD server side.

        ```bash
        kubectl apply -f etcd.yaml
        ```

        A return result similar to the following indicates that the creation is successful:

        ```txt
        service/etcd created
        statefulset.apps/etcd created
        ```

    6. Run the following command to query the Pods of the ETCD cluster.

        ```bash
        kubectl get pod -A
        ```

        The output is as follows:

        ```txt
        NAMESPACE       NAME    READY   STATUS   RESTARTS    AGE IP               NODE          NOMINATED NODE   READINESS GATES
        default         etcd-0  1/1     Running  0           44h xxx.xxx.xxx.xxx  ubuntu        <none>           <none>
        default         etcd-1  1/1     Running  0           44h xxx.xxx.xxx.xxx  worker-153    <none>           <none>
        default         etcd-2  1/1     Running  0           44h xxx.xxx.xxx.xxx  worker-80-39  <none>           <none>
        ```

        >[!NOTE]NOTE
        >If you want to modify the .yaml file in the ETCD cluster and recreate the ETCD resources, you need to delete them first. The command is as follows:<br>
        > ```kubectl delete -f etcd.yaml && kubectl delete pvc --all && kubectl delete pv etcd-data-0 etcd-data-1 etcd-data-2```<br>
        > Then delete the contents in the etcd-0, etcd-1, and etcd-2 databases:
        >
        > ```bash
        > rm -rf /mnt/data/etcd-0/*
        > rm -rf /mnt/data/etcd-1/*
        > rm -rf /mnt/data/etcd-2/*
        > ```
