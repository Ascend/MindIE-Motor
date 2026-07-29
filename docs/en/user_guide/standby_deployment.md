# Master/Standby Failover

The master/standby failover is primarily implemented via etcd distributed locks to ensure high system availability. It covers both Controller and Coordinator master-standby configurations.

# Master/Standby Failover of the Controller

## Feature Description

To ensure high system availability, this feature leverages the etcd distributed lock mechanism to implement Controller failover in the Kubernetes cluster. After the master/standby failover of the Controller is turned on, the system starts two Controller instances during initialization. The etcd distributed lock is used to determine the master and standby roles of the Controller. When the master Controller is faulty, the standby Controller automatically takes over services after a specified period of time.

**Restrictions**

- You are not advised to deploy the master and standby Controllers on the same node.
- The etcd server requires v3.6.
- This feature takes effect only when the etcd server is correctly deployed. The server requires at least three replicas to ensure reliability of the etcd cluster.
- The master/standby failover feature of Coordinator and Controller can share the same etcd. Multiple MoE EP clusters can share the same etcd and are distinguished by namespace.

## Deployment Process

### (Optional) Generating an etcd Security Certificate

The master/standby failover of the Controller depends on the etcd distributed lock function, which involves communication between different pods in the cluster. You are advised to use the CA certificate for two-way authentication. For details about how to configure the certificate, see [Generating the etcd Security Certificate](#optional-generating-an-etcd-security-certificate).
>[!NOTE]NOTE
>If the CA certificate is not used for two-way authentication and encrypted communication, services will be transmitted in plaintext, and high network security risks may exist.

### Deploying the etcd Server

Only one etcd server needs to be deployed. For details, see [Deploying etcd](#deploying-the-etcd-server).

### (Optional) Configuring the Kubernetes Management End

When a hardware fault (for example, machine restart) occurs, the Kubernetes cluster cannot quickly detect the container pod status. As a result, the inference service cannot be restored within the specified time. You can perform the following steps to speed up service restoration.

>[!NOTE]NOTE
>If the impact duration of a hardware fault is not required, skip the following steps.

1. Run the following command to query the heartbeat timeout flag threshold (`node-monitor-grace-period`) of the Kubernetes management node. If the command output is empty, the default value is used.

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

2. Run the following command to open and modify the heartbeat timeout flag threshold (`node-monitor-grace-period`) of the configured node. Generally, the configuration file is stored in the `/etc/kubernetes/manifests/kube-controller-manager.yaml` directory on the control plane node (on which the kube-controller-manager is running).

    ```bash
    vi /etc/kubernetes/manifests/kube-controller-manager.yaml
    ```

      The modified content is as follows:

      ```yaml
        apiVersion: v1
        kind: Pod
        metadata:
        name: kube-controller-manager-<Control plane node name >  # For example, kube-controller-manager-node-97-10
        namespace: kube-system
        spec:
        containers:
        - command:
            - kube-controller-manager
            # Other original parameters... (Retain the original values)
            - --kubeconfig=/etc/kubernetes/controller-manager.conf
            - --authentication-kubeconfig=/etc/kubernetes/controller-manager.conf
            # Add or modify the node-monitor-grace-period parameter (change the value to 20s)
            - --node-monitor-grace-period=20s
            # Other original parameters...
      ```

3. Press `Esc`, type `:wq!`, and press `Enter` to save the file and exit.
4. Run the following command to restart the Kubernetes service on the node where `kube-controller-manager` is deployed to restart the `kube-controller-manager` service:

    ```bash
    systemctl restart kubelet.service
    ```

5. Run the following command to check whether the parameters take effect:

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

    If the following information is printed, the parameters have taken effect:

    ```bash
    --node-monitor-grace-period=20s
    ```

### Configuring the Motor

1. Configure certificate mounting on the Controller.

    <b>If the CA certificate is not enabled, skip this step.</b><br>
    If you need to enable CA certificate authentication, mount the path where the certificate file is generated to the Controller container based on the certificate file generated in [3.1](#optional-generating-an-etcd-security-certificate). Add the following content to `volumeMounts` and `volumes` in the `deployment/controller_init.yaml` file (`controller-ca` is the mounted certificate directory):

    ```yaml
    ...
          volumeMounts:
          ...
          - name: controller-ca
            mountPath: /usr/local/Ascend/pyMotor/conf/security/etcd # Container path to which the /home/{Username}/auto_gen_ms_cert directory on the physical machine is mounted
      volumes:
      ...
      - name: controller-ca
        hostPath:
          path: /home/{User name}/auto_gen_ms_cert # Path for creating and generating files on the physical machine
          type: Directory
    ...
    ```

2. Configure the `user_config.json` file to enable TLS authentication.
    
    <b>If the CA certificate is not enabled, skip this step.</b><br>
    Enable CA certificate authentication:
    - Set `enable_tls` of `tls_config/etcd_tls_config` to `true`.
    - Set `ca_file`, `cert_file`, `key_file`, `passwd_file`, and `tls_crl` to the respective file paths.

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

3. In the `user_config.json` configuration file, enable the master/standby failover feature of the Controller. Configure the parameters as follows: Change the value of `"enable_master_standby"` to `true`.

    ```json
    ...
       "motor_Controller_config": {
          "standby_config": {
             "enable_master_standby": true
          }
       }
    ...
    ```

    - `false`: Disable the failover.
    - `true`: Enable the failover.
    >[!NOTE]NOTE
    > By default, the etcd server in the default workspace is used, and the default port number is `2379`. If you need to modify the etcd information, modify it in `motor_controller_config`. The domain name is usually `etcd.{namespace}.svc.cluster.local`.
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

### Starting vLLM

1. Run the following command in the `examples/deployer` directory to start the service: You can specify a configuration directory or a configuration file separately.

    ```bash
    cd examples/deployer
    # (Recommended) Method 1: Specify a configuration directory
    python deploy.py --config_dir ../infer_engines/vllm

    # Method 2: Specify the configuration file separately.
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    >[!NOTE]NOTE
    > * Check the logs on each node to identify the master/standby Controller. A log entry containing `"Role changed from standby to master"` indicates that the node has acquired the etcd distributed lock and is acting as the master node.<br>
    > * Use `kubectl get pod -A -owide` to list all pods. Exactly one Controller pod should have a `READY` status of `1/1`, indicating that it is the master Controller.

2. Send a request to check whether the service is started successfully.

    Use either of the following methods to send a request:
    
    - Virtual IP address and port number of the master Controller: `http://PodIP:1025` (Only the pod whose `READY` is `1/1` can execute the inference request.)
    - IP address of any physical machine in the Kubernetes cluster: `31015` (The port number must be the same as the `nodePort` of `mindie-motor-coordinator-infer` in `examples/deployer/yaml_template/coordinator_template.yaml` (multi_deployment scenario) or `examples/deployer/yaml_template/infer_service_template.yaml` (CRD scenario).)

    Example of using the IP address and port number of the physical machine:

    ```json
    #!/bin/bash
    url="http://{IP address of the physical machine}:31015/v1/chat/completions"
    data='{
        "model": "deepseek",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "Who are you?"}]
    }'
    curl  $url -X POST  -d "$data"
    ```

    If the following information is displayed, the inference service is started successfully:

    ```txt
    ...
       "message": {
          "role": "assistant",
          "content": "<think>\n OK, the user asked who I am. I",
       ...
       }
    ...
    ```

# Master/Standby Failover of the Coordinator

## Feature Description

To ensure high system availability, this feature leverages the etcd distributed lock mechanism to implement master/standby switchover of the Coordinator in the Kubernetes cluster. When the switch controlling master/standby failover of the Coordinator is turned on, two Coordinator nodes are started during initialization. The etcd distributed lock is used to determine the master and standby roles of the Coordinator. When the master Coordinator is faulty, the standby Coordinator automatically takes over services after a certain period of time.

**Restrictions**

- You are not advised to deploy the master and standby Coordinators on the same node.
- The etcd server requires v3.6.
- This feature takes effect only when the etcd server is correctly deployed. The server requires at least three replicas to ensure reliability of the etcd cluster.
- The master/standby failover feature of Coordinator and Controller can share the same etcd. Multiple MoE EP clusters can share the same etcd and are distinguished by namespace.

## Deployment Process

### (Optional) Generating an etcd Security Certificate

The master/standby failover of the Coordinator depends on the etcd distributed lock function, which involves communication between different pods in the cluster. You are advised to use the CA certificate for two-way authentication. For details about how to configure the certificate, see [Generating the etcd Security Certificate](#optional-generating-an-etcd-security-certificate).
<b>If the CA certificate is not enabled, skip this step.</b>

### Deploying the etcd Server

For details about how to deploy the etcd server, see [Deploying etcd](#deploying-the-etcd-server).
>[!NOTE]NOTE
>The master/standby failover feature of Coordinator and Controller can share the same etcd. Multiple MoE EP clusters can share the same etcd and are distinguished by namespace.

### (Optional) Configuring the Kubernetes Management End

When a hardware fault (for example, machine restart) occurs, the Kubernetes cluster cannot quickly detect the container pod status. As a result, the inference service cannot be restored within the specified time. You can perform the following steps to speed up service restoration.

>[!NOTE]NOTE
>If the impact duration of a hardware fault is not required, skip the following steps.

1. Run the following command to query the heartbeat timeout flag threshold (`node-monitor-grace-period`) of the Kubernetes management node. If the command output is empty, the default value is used.

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

2. Run the following command to open and modify the heartbeat timeout flag threshold (`node-monitor-grace-period`) of the configured node. Generally, the configuration file is stored in the `/etc/kubernetes/manifests/kube-controller-manager.yaml` directory on the control plane node (on which the kube-controller-manager is running).

    ```bash
    vi /etc/kubernetes/manifests/kube-controller-manager.yaml
    ```

      The modified content is as follows:

      ```yaml
        apiVersion: v1
        kind: Pod
        metadata:
        name: kube-controller-manager-<Control plane node name >  # For example, kube-controller-manager-node-97-10
        namespace: kube-system
        spec:
        containers:
        - command:
            - kube-controller-manager
            # Other original parameters... (Retain the original values)
            - --kubeconfig=/etc/kubernetes/controller-manager.conf
            - --authentication-kubeconfig=/etc/kubernetes/controller-manager.conf
            # Add or modify the node-monitor-grace-period parameter (change the value to 20s)
            - --node-monitor-grace-period=20s
            # Other original parameters...
      ```

3. Press `Esc`, type `:wq!`, and press `Enter` to save the file and exit.
4. Run the following command to restart the Kubernetes service on the node where `kube-controller-manager` is deployed to restart the `kube-controller-manager` service:
    
    ```bash
    systemctl restart kubelet.service
    ```

5. Run the following command to check whether the parameters take effect:

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

    If the following information is printed, the parameters have taken effect:

    ```txt
    --node-monitor-grace-period=20s
    ```

### Configuring the Motor

1. Configure certificate mounting on the Coordinator.

    <b>If the CA certificate is not enabled, skip this step.</b><br>
    If you need to enable CA certificate authentication, mount the path where the certificate file is generated to the Coordinator container based on the certificate file generated in [3.1](#optional-generating-an-etcd-security-certificate). Add the following content to `volumeMounts` and `volumes` in the `examples/deployer/yaml_template/coordinator_template.yaml` file (`coordinator-ca` is the mounted certificate directory):

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
            mountPath: /usr/local/Ascend/pyMotor/conf/security/etcd # Container path to which the /home/{Username}/auto_gen_ms_cert directory on the physical machine is mounted
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
          path: /home/{User name}/auto_gen_ms_cert # Path for creating and generating files on the physical machine
          type: Directory
    ...
    ```

2. Configure the `user_config.json` file to enable TLS authentication.<br>
    <b>If the CA certificate is not enabled, skip this step.</b><br>
    Enable CA certificate authentication:
    - Set `enable_tls` of `tls_config/etcd_tls_config` to `true`.
    - Set `ca_file`, `cert_file`, `key_file`, `passwd_file`, and `tls_crl` to the respective file paths.
    
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

3. In the `user_config.json` configuration file, enable the master/standby failover feature of the Coordinator. Configure the parameters as follows: Change the value of `"enable_master_standby"` to `true`.

    ```json
    ...
       "motor_coordinator_config": {
          "standby_config": {
             "enable_master_standby": true
          }
       }
    ...
    ```

    - `false`: Disable the failover.
    - `true`: Enable the failover.
    >[!NOTE]NOTE
    > By default, the etcd server in the default workspace is used, and the default port number is `2379`. If you need to modify the etcd information, modify it in `motor_coordinator_config`. The domain name is usually `etcd.{namespace}.svc.cluster.local`.
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

### Starting vLLM

1. Run the following command in the `examples/deployer` directory to start the service: You can specify a configuration directory or a configuration file separately.

    ```bash
    cd examples/deployer
    # (Recommended) Method 1: Specify a configuration directory
    python deploy.py --config_dir ../infer_engines/vllm

    # Method 2: Specify the configuration file separately.
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    >[!NOTE]NOTE
    > * Check the logs on each node to identify the master/standby Coordinator. A log entry containing `"Role changed from standby to master"` indicates that the node has acquired the etcd distributed lock and is acting as the master node.
    > * Use `kubectl get pod -A -owide` to list all pods. Exactly one Coordinator pod should have a `READY` status of `1/1`, indicating that it is the master Coordinator.

2. Send a request to check whether the service is started successfully.

    Use either of the following methods to send a request:
    - Virtual IP address and port number of the master Coordinator: `http://PodIP:1025` (Only the pod whose `READY` is `1/1` can execute the inference request.)
    - IP address of any physical machine in the Kubernetes cluster: `31015` (The port number must be the same as the `nodePort` of `mindie-motor-coordinator-infer` in `examples/deployer/yaml_template/coordinator_template.yaml` (multi_deployment scenario) or `examples/deployer/yaml_template/infer_service_template.yaml` (CRD scenario).)

    In this example, the IP address and port number of the physical machine are used.

    ```json
    #!/bin/bash
    url="http://{IP address of the physical machine}:31015/v1/chat/completions"
    data='{
        "model": "deepseek",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "Who are you?"}]
    }'
    curl  $url -X POST  -d "$data"
    ```

    If the following information is displayed, the inference service is started successfully:
    
    ```json
    ...
       "message": {
          "role": "assistant",
          "content": "<think>\n OK, the user asked who I am. I",
       ...
       }
    ...
    ```

# Deploying an etcd Cluster

etcd cluster deployment consists of two parts: generating etcd security certificates and deploying the etcd server.

## (Optional) Generating an etcd Security Certificate

>[!NOTE]NOTE
>If the CA certificate is not used for two-way authentication and encrypted communication, services will be transmitted in plaintext, and high network security risks may exist.

1. Prepare the required certificate files in advance and place them under a directory such as `/home/{username}/auto_gen_ms_cert`.

    **server.cnf**

    ```txt
    [req] # Main request content
    req_extensions = v3_req
    distinguished_name = req_distinguished_name

    [req_distinguished_name] # Certificate body information
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

    [alt_names] # Service ID
    DNS.1 = etcd
    DNS.2 = etcd.default
    DNS.3 = etcd.default.svc
    DNS.4 = etcd.default.svc.cluster.local  # etcd must be deployed in the default namespace.
    DNS.5 = etcd-0.etcd
    DNS.6 = etcd-0.etcd.default.svc.cluster.local
    DNS.7 = etcd-1.etcd
    DNS.8 = etcd-1.etcd.default.svc.cluster.local
    DNS.9 = etcd-2.etcd
    DNS.10 = etcd-2.etcd.default.svc.cluster.local
    ```

    **client.cnf**

    ```txt
    [req] # Main request content
    req_extensions = v3_req
    distinguished_name = req_distinguished_name

    [req_distinguished_name] # Certificate body information
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

    [alt_names] # Service ID
    DNS.1 = mindie-service-controller
    DNS.2 = mindie-service-coordinator

    ```

    **crl.conf**

    ```txt
    # OpenSSL configuration for CRL generation
    #
    ####################################################################
    [ ca ] # CA framework declaration, indicating the predefined CA configuration block used by OpenSSL as the default setting
    default_ca = CA_default # The default ca section
    ####################################################################
    [ CA_default ] # Core CA settings, including all key paths, files, and default operations
    dir             = {dir}  # Add this root directory definition, e.g., /home/{username}/auto_gen_ms_cert
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
    >It is recommended that the `{dir}` path in the file be a shared directory that can be accessed by each node.

    **gen_etcd_controller_ca.sh**

    ```bash
    #!/bin/bash
    # Configure the base directory, which corresponds to `crl.conf`.
    base_dir=/home/{username}/auto_gen_ms_cert
    # 1. Create the required files and directories
    mkdir -p ${base_dir}/etcd_crl/newcerts
    touch ${base_dir}/etcd_crl/index.txt
    echo 1000 > ${base_dir}/etcd_crl/pulp_crl_number
    echo "01" > ${base_dir}/etcd_crl/serial
    # 2. Set permissions
    chmod 700 ${base_dir}/etcd_crl/newcerts
    chmod 600 ${base_dir}/etcd_crl/{index.txt,pulp_crl_number,serial}
    # 3. Create a CA certificate
    openssl genrsa -aes256 -out ca.key 4096
    openssl req -x509 -new -nodes -key ca.key \
    -subj "/CN=my-cluster-ca" \
    -days 3650 -out ca.pem
    # 4. Generate a server certificate
    openssl genrsa -out server.key 4096
    openssl req -new -key server.key -out server.csr \
    -subj "/CN=etcd-server" -config server.cnf
    openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out server.pem -days 3650 -extensions v3_req -extfile server.cnf
    # 5. Generate a client certificate
    openssl genrsa -out client.key 4096
    openssl req -new -key client.key -out client.csr \
    -subj "/CN=inst0-client" -config client.cnf
    openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out client.pem -days 3650 -extensions v3_req -extfile client.cnf
    # 6. Configure permissions.
    chmod 0400 ./*.key
    chmod 0400 ./*.pem
    ```

    >[!NOTE]NOTE
    > Remember the password used to generate `ca.key`, which will be required when generating a new certificate.

2. Run `gen_etcd_controller_ca.sh` using the following command to generate files such as the server certificate and client certificate.

    ```bash
    bash gen_etcd_controller_ca.sh
    ```

    If information similar to the following is displayed, the generation is successful:

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

    After the command is successfully executed, the following files or directories are generated in the current directory.

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

    After completing the above steps, a CA certificate, a server certificate, and a client certificate are generated. (The CA certificate is used for authentication, the server certificate for etcd cluster deployment, and the client certificate for Controller/Coordinator master/standby failover.)

    >[!NOTE]NOTE
    > If multiple client certificates are required, use the same CA certificate and repeat the following operations:
    >
    > ```bash
    > openssl genrsa -out {New client}.key 4096
    > openssl req -new -key {New client}.key -out {New client}.csr \
    > -subj "/CN={CN of the new client}" -config client.cnf
    > openssl x509 -req -in {New client}.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    > -out {New client}.pem -days 3650 -extensions v3_req -extfile client.cnf
    > # Modify the permission on the newly generated certificate
    > chmod 0400 ./*.key
    > chmod 0400 ./*.pem
    >  ```

## Deploying the etcd Server

The following is an example of the deployment.

1. Run the following command to download the image.

    ```bash
    docker pull quay.io/coreos/etcd:v3.6.0-rc.4
    ```

    >[!NOTE]NOTE
    >If `docker pull` fails, you can use `podman` to download the etcd image, save it, and then import it with `docker load`.
    >
    > ```bash
    > apt install podman
    > podman pull quay.io/coreos/etcd:v3.6.0-rc.4
    > ```
    >
    > etcd must be deployed with at least three replicas. Import the image to the specified node.

2. Create etcd resources in the cluster.

    1. Create the `local-pvs.yaml` file.

        ```bash
        vim local-pvs.yaml
        ```

        Write the following information to the file:
        
        ```yaml
        # Create a PV in local-pvs.yaml
        apiVersion: v1
        kind: PersistentVolume
        metadata:
          name: etcd-data-0  # The value must comply with the PVC naming rule of the StatefulSet
        spec:
          capacity:
            storage: 4096M
          volumeMode: Filesystem
          accessModes: [ReadWriteOnce]
          persistentVolumeReclaimPolicy: Retain
          storageClassName: local-storage  # The value must match storageClass of the PVC
          local:
            path: /mnt/data/etcd-0  # Actual path on the node
          nodeAffinity:
            required:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: kubernetes.io/hostname
                      operator: In
                      values: ["ubuntu"]  # Bound to a specific node, that is, NodeName.

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
                      values: ["worker-80-39"] # Bound to a specific node, that is, NodeName.

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
                      values: ["worker-153"] # Bound to a specific node, that is, NodeName.
        ```

        Key parameters are as follows:
        - `spec.local.path`: path to the corresponding node, which must exist.
        - `spec.nodeAffinity.required.nodeSelectorTerms.matchExpressions.values`: name of the node to be deployed.

    2. Run the following command on the master node of the Kubernetes cluster to create a PVS:
        
        ```bash
        kubectl apply -f local-pvs.yaml
        ```
        
        If the following information is displayed, the creation is successful:
        
        ```bash
        persistentvolume/etcd-data-0 created
        persistentvolume/etcd-data-1 created
        persistentvolume/etcd-data-2 created
        ```

    3. Run the following command to label the three nodes with `app=etcd`:
        
        ```bash
        kubectl label nodes <Node name > app=etcd
        ```

        If the following information is displayed, the creation is successful:

        ```bash
        node/<Node name > labeled
        ```

    4. Run the following command to create the `etcd.yaml` file and configure the etcd certificate on the pod:
        
        ```bash
        vim etcd.yaml
        ```

        Based on the certificate generated in [3.1](standby_deployment.md#optional-generating-an-etcd-security-certificate), mount the generated file path into the etcd container, and configure etcd to use encrypted communication with `ca.pem`, `server.pem`, and `server.key`.
        
        ```yaml
        # Create a synchronized etcd database on the three nodes in the etcd.yaml file
        ---
        apiVersion: v1
        kind: Service
        metadata:
          name: etcd
          namespace: default
        spec:
          type: ClusterIP
          clusterIP: None # Headless Service, which is used for DNS resolution of StatefulSet
          selector:
            app: etcd  # Select the pods labeled with app=etcd
          publishNotReadyAddresses: true  # Allow the DNS to discover the pod that is not ready
          ports:
            - name: etcd-client
              port: 2379 # Client communication port
            - name: etcd-server
              port: 2380 # Inter-node communication port
            - name: etcd-metrics
              port: 8080 # etcd cluster management and control port
        ---
        apiVersion: apps/v1
        kind: StatefulSet
        metadata:
          name: etcd
          namespace: default
        spec:
          serviceName: etcd # Bind Headless Service
          replicas: 3 # The number of nodes must be an odd number to ensure Raft
          podManagementPolicy: OrderedReady # Parallel startup is allowed (requiring cooperation with the initialization script)
          updateStrategy:
            type: RollingUpdate # Rolling update policy
          selector:
            matchLabels:
              app: etcd # Match the pod label
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
                      topologyKey: "kubernetes.io/hostname" # Cross-node deployment
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
                    - --name=$(HOSTNAME) # Unique node ID
                    - --data-dir=/data # Data storage path
                    - --wal-dir=/data/wal
                    - --listen-peer-urls=https://0.0.0.0:2380 # Listen to inter-node communication
                    - --listen-client-urls=https://0.0.0.0:2379 # Monitor client requests
                    - --advertise-client-urls=https://$(HOSTNAME).$(SERVICE_NAME):2379  # Client address
                    - --initial-cluster-state=new # Initialization mode of the new cluster
                    - --initial-cluster-token=etcd-$(K8S_NAMESPACE) # Unique ID of the cluster
                    - --initial-cluster=etcd-0=https://etcd-0.etcd:2380,etcd-1=https://etcd-1.etcd:2380,etcd-2=https://etcd-2.etcd:2380 # Initial node list
                    - --initial-advertise-peer-urls=https://$(HOSTNAME).$(SERVICE_NAME):2380 # Public IP address for inter-node communication
                    - --listen-metrics-urls=http://0.0.0.0:8080 # Cluster management and control address
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
                      mountPath: /etc/ssl/certs/etcdca # Container path to which the /home/{Username}/auto_gen_ms_cert directory on the physical machine is mounted
              volumes:
                - name: crt
                  hostPath:
                    path: /usr/local/Ascend/driver
                - name: etcd-ca
                  hostPath:
                    path: /home/{User name}/auto_gen_ms_cert # Path for creating and generating files on the physical machine
                    type: Directory
          volumeClaimTemplates:
            - metadata:
                name: etcd-data
              spec:
                accessModes: [ "ReadWriteOnce" ] # Read and write on a single node
                storageClassName: local-storage
                resources:
                  requests:
                    storage: 4096M # Storage space
        ```

        Key parameters are as follows:

        - `spec.template.spec.containers.args.--client-cert-auth`: enables client certificate authentication.
        - `spec.template.spec.containers.args.--cert-file`: specifies the server certificate.
        - `spec.template.spec.containers.args.--key-file`: specifies the server private key.
        - `spec.template.spec.containers.args.--trusted-ca-file`: specifies the trusted CA root certificate.
        - `spec.template.spec.containers.args.--peer-client-cert-auth`: enables client certificate authentication between peers.
        - `spec.template.spec.containers.args.--peer-trusted-ca-file`: specifies the trusted CA root certificate (for peers).
        - `spec.template.spec.containers.args.--peer-cert-file`: specifies the certificate file of the peer node.
        - `spec.template.spec.containers.args.--peer-key-file`: specifies the private key of the peer node.

    5. Run the following command on the master node of the Kubernetes cluster to deploy the etcd server:
        
        ```bash
        kubectl apply -f etcd.yaml
        ```

        If the following information is displayed, the creation is successful:

        ```txt
        service/etcd created
        statefulset.apps/etcd created
        ```

    6. Run the following command to query pods of the etcd cluster:

        ```bash
        kubectl get pod -A
        ```

        The command output is similar to the following:
        
        ```txt
        NAMESPACE       NAME    READY   STATUS   RESTARTS    AGE IP               NODE          NOMINATED NODE   READINESS GATES
        default         etcd-0  1/1     Running  0           44h xxx.xxx.xxx.xxx  ubuntu        <none>           <none>
        default         etcd-1  1/1     Running  0           44h xxx.xxx.xxx.xxx  worker-153    <none>           <none>
        default         etcd-2  1/1     Running  0           44h xxx.xxx.xxx.xxx  worker-80-39  <none>           <none>
        ```

        >[!NOTE]NOTE
        >To modify the YAML file in the etcd cluster and re-create etcd resources, run the following command to delete the resources:<br>
        > `kubectl delete -f etcd.yaml && kubectl delete pvc --all && kubectl delete pv etcd-data-0 etcd-data-1 etcd-data-2`<br>
        > Delete the content in the etcd-0, etcd-1, and etcd-2 databases.
        > 
        > ```bash
        > rm -rf /mnt/data/etcd-0/*
        > rm -rf /mnt/data/etcd-1/*
        > rm -rf /mnt/data/etcd-2/*
        > ```
