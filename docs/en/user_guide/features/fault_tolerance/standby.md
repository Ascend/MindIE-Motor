# Active/Standby Switchover Feature

## Feature Introduction

The active/standby switchover feature is implemented primarily through ETCD distributed locks to ensure high availability of the system, including active/standby Controller and active/standby Coordinator. After the active/standby switchover feature switch is enabled, the system starts two instances during the initialization phase and performs active/standby identity election through ETCD distributed lock contention. When the active node fails, the standby node automatically takes over after a configured time interval.

After the active/standby switchover feature is enabled, the Controller and the Coordinator each start two Pods. The active node provides services externally, and the standby node does not carry service traffic and only participates in leader election. The Controller and the Coordinator use independent ETCD lock keys, and the elections of the two components do not affect each other. You can enable only one of them or both.

### Constraints and Limitations

| Constraint Dimension | Requirement |
|----------|------|
| Hardware | No specific hardware model is required. When this feature is used together with capabilities such as fault awareness, you are advised to explicitly configure `hardware_type` in `motor_deploy_config`. |
| Deployment scenario | <ul><li>It is not recommended to deploy the active and standby nodes (Controller/Coordinator) on the same node; the deployment template has enabled mandatory anti-affinity scheduling by default (`podAntiAffinity`, `topologyKey: kubernetes.io/hostname`), so active and standby Pods are automatically distributed to different nodes.</li><li>After the active/standby switchover is enabled, the deployment tool automatically sets the number of Pod replicas (`replicas`) of the Controller/Coordinator to 2. You do not need to manually scale out replicas.</li></ul> |
| Engine | Not involved. The feature itself does not depend on a specific inference engine, and engines such as vLLM and SGLang are supported. |
| Feature mutual exclusion | No explicit mutual exclusion is described. This feature can coexist with other features. |
| Software dependency | The ETCD server side must use v3.6. |
| Other constraints | <ul><li>The feature takes effect only when the ETCD server side is correctly deployed. The server side requires at least three replicas to ensure the reliability of the ETCD cluster;</li><li>The Coordinator and Controller active/standby switchover features can share one ETCD. Multiple large-scale EP clusters can share one ETCD, distinguished by namespace;</li><li>After the active/standby switchover is enabled, the system automatically enables ETCD persistence (`etcd_config.enable_etcd_persistence`). The standby node relies on the persisted data in ETCD to restore the service state after switchover;</li><li>The active/standby switchover needs to wait for the ETCD lease to time out. During the switchover, a service unavailability window of several seconds exists, and the service side must support retries.</li></ul> |

## Feature Usage

### Environment Preparation

The active/standby switchover depends on the ETCD distributed lock feature, which involves communication between different PODs in the cluster. It is recommended to use CA certificates for mutual authentication. ETCD cluster deployment consists of two parts: generating ETCD security certificates and deploying the ETCD server. Only one set of ETCD server side needs to be deployed, and the Coordinator and Controller active/standby switchover features can share one ETCD.

#### (Optional) Generating ETCD Security Certificates

>[!NOTE]NOTE
>If CA certificates are not used for mutual authentication and encrypted communication, services will transmit data in plaintext, which may pose a high network security risk.

1. Prepare the prerequisite files for certificate generation. The following example uses `/home/{username}/auto_gen_ms_cert` as the directory for storing the files.

    **server.cnf**

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

    >[!NOTE]NOTE
    >The domain names in `alt_names` must cover `etcd_config.etcd_host` configured for the Controller/Coordinator and the domain names of all ETCD cluster nodes. Otherwise, after TLS is enabled, the client cannot connect to ETCD because certificate verification fails.

    **client.cnf**

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

    **crl.conf**

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

    **gen_etcd_controller_ca.sh**

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

#### Deploying the ETCD Server

The following is a deployment reference example.

1. Run the following command to load the ETCD image.

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

    2. Run the following command on the master node of the K8s cluster to create pvs.

        ```bash
        kubectl apply -f local-pvs.yaml
        ```

        If the following result is returned, the pvs are created successfully:

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

        Based on the certificates generated in [Generating ETCD Security Certificates](#optional-generating-etcd-security-certificates), mount the generation path of the files into the ETCD container, configure ETCD to use encrypted communication, and specify `ca.pem`, `server.pem`, and `server.key` for communication.

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

        The key parameters are described as follows:

        - `spec.template.spec.containers.args.--client-cert-auth`: enable client certificate authentication

        - `spec.template.spec.containers.args.--cert-file`: specify the server certificate

        - `spec.template.spec.containers.args.--key-file`: specify the server private key

        - `spec.template.spec.containers.args.--trusted-ca-file`: specify the trusted CA root certificate

        - `spec.template.spec.containers.args.--peer-client-cert-auth`: enable client certificate authentication between peer nodes

        - `spec.template.spec.containers.args.--peer-trusted-ca-file`: specify the trusted CA root certificate (for peers)

        - `spec.template.spec.containers.args.--peer-cert-file`: specify the certificate of this node as a peer

        - `spec.template.spec.containers.args.--peer-key-file`: specify the private key of this node as a peer

        >[!NOTE]NOTE
        >The TLS parameters in the example, such as `--client-cert-auth` and `--peer-client-cert-auth`, need to be retained only when [Generating ETCD Security Certificates](#optional-generating-etcd-security-certificates) has been performed. If CA certificates are not used, delete the preceding TLS-related parameters and the `etcd-ca` volume; otherwise, ETCD fails to start because the certificate files cannot be found.

    5. Run the following command on the master node of the K8s cluster to deploy the ETCD server side.

        ```bash
        kubectl apply -f etcd.yaml
        ```

        The following result indicates that the creation is successful:

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
        >- Which method to use to make the modification take effect after modifying `etcd.yaml` depends on the field being modified:
        >
        >   - If only fields that can take effect through a rolling update, such as `image`, `env`, and `args`, are modified, the StatefulSet uses the RollingUpdate strategy and starts each Pod in sequence without deleting PVCs. Run the following command.
        >
        >      ```bash
        >      kubectl -n <namespace> apply -f etcd.yaml
        >      ```
        >
        >   - If structural fields such as `--name`, `--initial-cluster`, `--data-dir`, `replicas`, `storageClassName`, `nodeAffinity`, and `local.path` are modified, the ETCD cluster must be rebuilt. The procedure is as follows.
        >
        >      ```bash
        >      # 1. Delete the Service and StatefulSet (PVCs are not deleted at the same time)
        >      kubectl -n <namespace> delete -f etcd.yaml
        >      # 2. Precisely delete the three PVCs of this ETCD (naming rule: <volumeClaimTemplate name>-<StatefulSet name>-<ordinal>)
        >      kubectl -n <namespace> delete pvc etcd-data-etcd-0 etcd-data-etcd-1 etcd-data-etcd-2
        >      # 3. Delete the corresponding PVs (PVs use the Retain policy and are in the Released state after the PVCs are deleted; only then can they be deleted)
        >      kubectl -n <namespace> delete pv etcd-data-0 etcd-data-1 etcd-data-2
        >      ```
        >
        >- Do not use the `kubectl delete pvc --all` command. This command deletes all PVCs in the current namespace and may delete storage volumes used by other services (for example, `mindie-motor-store-*` used by features such as KV pooling), causing unrecoverable data loss.
        >- To back up data, run the following command before deletion (adjust the TLS parameters based on the actual certificate paths):
        >
        >   ```bash
        >   kubectl -n <namespace> exec etcd-0 -- etcdctl --endpoints=https://etcd-0.etcd:2379 --cacert=/etc/ssl/certs/etcdca/ca.pem --cert=/etc/ssl/certs/etcdca/server.pem --key=/etc/ssl/certs/etcdca/server.key snapshot save /tmp/etcd-backup.db
        >   ```
        >
        >   The backup file is stored at `/tmp/etcd-backup.db` in the container. Run the following command to export it to the local host.
        >
        >   ```bash
        >   kubectl -n <namespace> cp <etcd-pod>:/tmp/etcd-backup.db ./etcd-backup.db
        >   ```
        >
        >- Then delete the contents in the etcd-0, etcd-1, and etcd-2 databases (before deletion, confirm that the backup is complete and that the directory is used only by this ETCD cluster):
        >
        >    ```bash
        >    rm -rf /mnt/data/etcd-0/*
        >    rm -rf /mnt/data/etcd-1/*
        >    rm -rf /mnt/data/etcd-2/*
        >    ```

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

      The modifications are as follows (this file is a static Pod manifest. Only append or modify the following parameter lines. Do not replace the entire file.):

      ```yaml
      # The following is only a fragment. Fields such as metadata, name, namespace, and spec all exist in the actual file. Keep the original indentation and content
      spec:
        containers:
        - command:
            - kube-controller-manager
            # Keep the preceding original parameters unchanged
            # Add/modify the node-monitor-grace-period parameter (change it to 20s)
            - --node-monitor-grace-period=20s
            # Keep the following original parameters unchanged
            ...
      ```

    >[!NOTE]NOTE
    >- Before modification, back up the original file: `cp /etc/kubernetes/manifests/kube-controller-manager.yaml /root/kube-controller-manager.yaml.bak`.
    >- This file is monitored by kubelet, which recreates the Pod based on it. Incorrect indentation levels of metadata, name, namespace, and spec cause `kube-controller-manager` to fail to start.

3. Press Esc, type `:wq!`, and press Enter to save and exit editing.

4. Run the following command to restart the K8s service on the node where `kube-controller-manager` resides, thereby restarting the `kube-controller-manager` service.

    ```bash
    systemctl restart kubelet.service
    ```

5. Run the following command to verify whether the parameter takes effect:

    ```bash
    kubectl describe pod <kube-controller-manager-pod name> -n kube-system | grep "node-monitor-grace-period"
    ```

    The following output indicates that the parameter has taken effect:

    ```txt
    --node-monitor-grace-period=20s
    ```

### Usage Scenarios

The active/standby switchover feature applies to scenarios that have requirements on control plane availability. The Controller and Coordinator active/standby switchover features are independent and can be enabled separately or together:

- Scenario 1: Controller active/standby switchover. The Controller is responsible for instance management, fault awareness, and instance distribution. A single-instance failure makes it impossible to detect the status of inference instances and to distribute instances to the Coordinator, which in turn makes the inference service unavailable. After the active/standby switchover is enabled, when the active Controller fails, the standby Controller automatically takes over after the configured time interval.

- Scenario 2: Coordinator active/standby switchover. The Coordinator carries the inference entry point. A single-instance failure directly causes inference requests to fail. After the active/standby switchover is enabled, when the active Coordinator fails, the standby Coordinator automatically takes over after the configured time interval, and inference traffic is switched to the new active node accordingly.

### Usage Example

#### Scenario 1: Controller Active/Standby Switchover

1. Configure certificate mounting on the Controller side. (If CA certificates are not enabled, skip this step.)

    If CA certificate authentication needs to be enabled, mount the generation path of the certificate files to the Controller container based on the certificate files generated in [Generating ETCD Security Certificates](#optional-generating-etcd-security-certificates). First, determine the template file to be modified based on `motor_deploy_config.deploy_mode` (the default mode is `infer_service_set`, which is consistent with the example commands in this document):

    | deploy_mode | File to Be Modified | Modification Position |
    |---|---|---|
    |infer_service_set (default)|examples/deployer/yaml_template/infer_service_template.yaml (CRD scenario)|spec.template.spec of name: controller under roles|
    |multi_deployment|examples/deployer/yaml_template/controller_template.yaml (multi_deployment scenario)|spec.template.spec in the Deployment|

    The following uses the `infer_service_set` deploy_mode as an example. Add the following content to `volumeMounts` and `volumes` in the `examples/deployer/yaml_template/infer_service_template.yaml` file (`controller-ca` is the mounted certificate directory):

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

    >[!NOTE]NOTE
    >- The mount point `/usr/local/Ascend/pyMotor/conf/security/etcd` is the path agreed in this document. The code does not have this default value, and the path must be consistent with the certificate path configured in `etcd_tls_config` in step 2.
    >- After the deployment is complete, run the following command to confirm that the certificate directory has been mounted to the generated yaml (for the infer_service_set mode, the corresponding file path is `examples/deployer/output_yamls/infer_service.yaml`; for the multi_deployment mode, the corresponding file path is examples/deployer/output_yamls/mindie_motor_controller.yaml, so as to avoid the certificate not being mounted due to a wrong template file)
    >
    >   ```bash
    >   grep -A3 controller-ca examples/deployer/output_yamls/infer_service.yaml
    >   ```

2. Configure the `user_config.json` configuration file to enable TLS authentication. (If CA certificates are not enabled, skip this step.)

    Enable CA certificate authentication:

    - Set `enable_tls` in `tls_config`/`etcd_tls_config` to `true`;

    - Set `ca_file`/`cert_file`/`key_file` to the corresponding file paths.

    ```json
    ...
      "tls_config": {
        ...
        "etcd_tls_config": {
          "enable_tls": true,
          "ca_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/ca.pem",
          "cert_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.pem",
          "key_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.key"
        },
        ...
      }
   ...
   ```

    >[!NOTE]NOTE
    >The Controller/Coordinator uses gRPC mutual authentication with ETCD. Only four fields take effect: `enable_tls`, `ca_file`, `cert_file`, and `key_file`. `passwd_file` and `crl_file` are reserved fields that do not take effect in ETCD scenarios and can keep their default values.

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
    >By default, the ETCD server side in the default namespace is used, and the default port number is 2379. To modify it, change the ETCD information in `motor_controller_config`. The domain name is usually etcd.{namespace}.svc.cluster.local.
    >
    > ```json
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
    >
    >- After the active/standby switchover is enabled, the system automatically enables ETCD persistence (`etcd_config.enable_etcd_persistence` is set to `true`). You do not need to configure it manually.
    >- The other parameters of `standby_config` (for example, `master_standby_check_interval` defaults to 5 seconds and `master_lock_ttl` defaults to 15 seconds) generally keep their default values. Increasing `master_lock_ttl` prolongs the switchover.

4. Run the following command in the `examples/deployer` directory to start the service. You can specify a configuration directory or specify a configuration file separately.

    ```bash
    cd examples/deployer
    # (Recommended) Method 1: Specify a configuration directory
    python deploy.py --config_dir ../../model_configs/vllm

    # Method 2: Specify a configuration file separately
    python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json
    ```

    After the deployment is complete, the number of Pod replicas of the Controller is 2.

5. Send a request to verify whether the service has started successfully.

    >[!NOTE]NOTE
    > The Controller provides only management plane interfaces (`motor_controller_config.api_config.controller_api_port`, default: 1026) and does not provide inference interfaces such as /v1/chat/completions. Inference requests must be sent to the active Coordinator node. To verify the active/standby state of the Controller, check the logs and the Pod READY status described in [Verification](#verification).

    There are two ways to send a request:

    - PodIP and port number of the active Coordinator node: `http://PodIP:1025`. (Only the Coordinator with a `READY` status of `1/1` can execute inference requests.)

    - Any physical machine IP in the K8s cluster: 31015 (the port number must be consistent with the nodePort of mindie-motor-coordinator-infer in examples/deployer/yaml_template/coordinator_template.yaml (multi_deployment scenario) or examples/deployer/yaml_template/infer_service_template.yaml (CRD scenario)).

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
          "content": "🤔\nOK, the user asks who I am. I",
       ...
       }
    ...
    ```

#### Scenario 2: Coordinator Active/Standby Switchover

1. Configure certificate mounting on the Coordinator side. (If CA certificates are not enabled, skip this step.)

    If CA certificate authentication needs to be enabled, mount the generation path of the certificate files to the Coordinator container based on the certificate files generated in [Generating ETCD Security Certificates](#optional-generating-etcd-security-certificates). First, determine the template file to be modified based on `motor_deploy_config.deploy_mode` (the default mode is `infer_service_set`, which is consistent with the example commands in this document):

    | deploy_mode | File to Be Modified | Modification Position |
    |---|---|---|
    |infer_service_set (default)|examples/deployer/yaml_template/infer_service_template.yaml (CRD scenario)|spec.template.spec of name: coordinator under roles|
    |multi_deployment|examples/deployer/yaml_template/coordinator_template.yaml (multi_deployment scenario)|spec.template.spec in the Deployment|

    The following uses the `multi_deployment` deploy_mode as an example. Add the following content to `volumeMounts` and `volumes` in the `examples/deployer/yaml_template/coordinator_template.yaml` file (`coordinator-ca` is the mounted certificate directory):

    ```yaml
    ...
          volumeMounts:
          - name: motor-config
            mountPath: /mnt/configmap
          - name: coredump
            mountPath: /var/coredump
          - name: mnt
            mountPath: /mnt
          - name: plog-path
            mountPath: /root/ascend/log
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
      - name: plog-path
        hostPath:
          path: /root/ascend/log
          type: DirectoryOrCreate
      - name: coordinator-ca
        hostPath:
          path: /home/{username}/auto_gen_ms_cert # Path for creating and generating files on the physical machine
          type: Directory
    ...
    ```

    >[!NOTE]NOTE
    >- The mount point `/usr/local/Ascend/pyMotor/conf/security/etcd` is the path agreed in this document. The code does not have this default value, and the path must be consistent with the certificate path configured in `etcd_tls_config` in step 2.
    >- After the deployment is complete, run the following command to confirm that the certificate directory has been mounted to the generated yaml (for the multi_deployment mode, the corresponding file path is examples/deployer/output_yamls/mindie_motor_coordinator.yaml; for the infer_service_set mode, the corresponding file path is examples/deployer/output_yamls/infer_service.yaml, so as to avoid the certificate not being mounted due to a wrong template file)
    >
    >   ```bash
    >   grep -A3 coordinator-ca examples/deployer/output_yamls/mindie_motor_coordinator.yaml
    >   ```

2. Configure the `user_config.json` configuration file to enable TLS authentication. (If CA certificates are not enabled, skip this step.)

    Enable CA certificate authentication:

    - Set `enable_tls` in `tls_config`/`etcd_tls_config` to `true`;

    - Set `ca_file`/`cert_file`/`key_file` to the corresponding file paths.

    ```json
    ...
      "tls_config": {
        ...
        "etcd_tls_config": {
          "enable_tls": true,
          "ca_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/ca.pem",
          "cert_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.pem",
          "key_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/client.key"
        },
        ...
      }
   ...
   ```

    >[!NOTE]NOTE
    >The Controller/Coordinator uses gRPC mutual authentication with ETCD. Only four fields take effect: `enable_tls`, `ca_file`, `cert_file`, and `key_file`. `passwd_file` and `crl_file` are reserved fields that do not take effect in ETCD scenarios and can keep their default values.

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
    >By default, the ETCD server side in the default namespace is used, and the default port number is 2379. To modify it, change the ETCD information in `motor_coordinator_config`. The domain name is usually etcd.{namespace}.svc.cluster.local.
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
    >
    >- After the active/standby switchover is enabled, the system automatically enables ETCD persistence (`etcd_config.enable_etcd_persistence` is set to `true`). You do not need to configure it manually.
    >- At runtime, the Coordinator automatically limits `master_lock_ttl` to no more than 8 seconds and `master_standby_check_interval` to no more than 2 seconds to ensure that the switchover completes within 30 seconds. Larger configured values do not take effect.

4. Run the following command in the `examples/deployer` directory to start the service. You can specify a configuration directory or specify a configuration file separately.

    ```bash
    cd examples/deployer
    # (Recommended) Method 1: Specify a configuration directory
    python deploy.py --config_dir ../../model_configs/vllm

    # Method 2: Specify a configuration file separately
    python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json
    ```

    After the deployment is complete, the number of Pod replicas of the Coordinator is 2.

5. Send a request to verify whether the service has started successfully.

    There are two ways to send a request:

    - PodIP and port number of the active Coordinator node: `http://PodIP:1025`. (Only the pod whose `READY` status is `1/1` can execute inference requests.)

    - Any physical machine IP in the K8s cluster: 31015 (the port number must be consistent with the nodePort of mindie-motor-coordinator-infer in examples/deployer/yaml_template/coordinator_template.yaml (multi_deployment scenario) or examples/deployer/yaml_template/infer_service_template.yaml (CRD scenario)).

    This sample uses the physical machine IP and port number method:

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

    ```json
    ...
       "message": {
          "role": "assistant",
          "content": "🤔\nOK, the user asks who I am. I",
       ...
       }
    ...
    ```

### Verification

After the service is started, you can verify whether the active/standby switchover feature takes effect in the following ways. After the active/standby switchover is enabled, the Controller and the Coordinator each have two Pods, and **exactly one Pod has a `READY` status of `1/1`** (the active node), while the other is `0/1` (the standby node).

**Method 1: Check the active/standby role (log-based)**

Query the logs of the corresponding node. If "Role changed from standby to master" appears in the log, it indicates that the current node has acquired the ETCD distributed lock and is the active node.

```bash
kubectl -n <namespace> logs <pod name> | grep "Role changed"
```

**Method 2: Check the active/standby role (K8s command-based)**

```bash
kubectl -n <namespace> get pod -owide
```

If exactly one Controller/Coordinator pod has a `READY` status of `1/1`, that node is the active node. A pod with a `READY` status of `0/1` is the standby node, which is expected.

**Method 3: Check the active/standby role (interface-based, optional)**

The Controller uses the management plane port (`motor_controller_config.api_config.controller_api_port`, default 1026), and the Coordinator uses the management and control plane port (`motor_coordinator_config.api_config.coordinator_api_mgmt_port`, default 1026). Access `/readiness`:

```bash
# Controller
curl http://{Controller PodIP}:1026/readiness
# Coordinator
curl http://{Coordinator PodIP}:1026/readiness
```

- The active Controller node returns 200: `{"message": "Controller is ready"}`; the standby node returns 503: `{"detail": {"message": "Controller is not ready", "reason": "Not master"}}`.

- The active Coordinator node returns 200: `{"status": "ok", "message": "Coordinator is master", "ready": true}`; the standby node returns 503: `{"detail": "Coordinator is not master"}`.

**Method 4: Switchover drill (optional)**

Delete the Pod of the active node to simulate an active node failure:

```bash
kubectl -n <namespace> delete pod <active node pod name>
```

Expected result: After the ETCD lease times out, the standby node successfully acquires the lock, "Role changed from standby to master" appears in the log, `READY` changes to `1/1`, and inference requests are handled by the new active node. The Coordinator takes about 10 seconds to become active (lease TTL 8 seconds + probe interval 2 seconds), and the Controller takes about 20 seconds (lease TTL 15 seconds + probe interval 5 seconds). During this period, inference requests may fail briefly. Confirm that retries are configured on the service side.

## FAQs

### Is It Normal That the READY of the Standby Node Stays at 0/1

**Symptom**

After the active/standby switchover is enabled, the Controller/Coordinator each have two Pods, one of which has a `READY` status of `0/1` for a long time.

**Cause Analysis**

In active/standby mode, only the active node provides services externally. The `/readiness` of the standby node returns 503 (the Coordinator returns `Coordinator is not master`, and the Controller returns `Not master`), based on which K8s marks the standby node as unready and removes it from the Service Endpoint. The process of the standby node itself runs properly, and its `/liveness` still returns 200.

**Solution**

This is expected behavior and no action is required. If the `READY` status of both Pods is `0/1`, no node has acquired the ETCD distributed lock. Refer to the next FAQ for troubleshooting.

### READY of Both Pods Is 0/1 and No Active Node Can Be Elected

**Symptom**

Both Pods of the Controller/Coordinator have a `READY` status of `0/1` for a long time, and the service is unavailable.

**Cause Analysis**

The standby node cannot acquire the ETCD distributed lock. Common causes include:

- The ETCD server side is not deployed or is unavailable;

- `etcd_config.etcd_host`/`etcd_port` is incorrectly configured, or the domain name cannot be resolved;

- TLS is enabled but the certificate path or certificate content is incorrect;

- The ETCD cluster loses the majority (more than one of the three replicas fails).

**Solution**

1. Confirm that the ETCD cluster is in a normal state:

    ```bash
    kubectl -n <namespace> get pod | grep etcd
    ```

2. Confirm that `etcd_config` in `user_config.json` is consistent with the actual ETCD deployment (the domain name is usually `etcd.{namespace}.svc.cluster.local`, and the port is 2379).

3. In the Controller/Coordinator container, confirm that ETCD is accessible, and check the container logs for records of ETCD connection failures or leader election failures.

### Controller/Coordinator Cannot Connect to ETCD After TLS Is Enabled

**Symptom**

The active/standby switchover works properly when TLS is disabled. After TLS is enabled, neither Pod can become active, and certificate-related errors appear in the container logs.

**Cause Analysis**

- The certificate path in the container is inconsistent with the path configured in `etcd_tls_config` (the deployment template does not mount the certificate directory by default, and the mount must be added manually);

- `alt_names` in `server.cnf` does not contain the domain name used by `etcd_config.etcd_host`, and certificate verification fails;

- The client certificates `cert_file`/`key_file` are not issued by the same CA.

**Solution**

1. Check that `ca_file`/`cert_file`/`key_file` in `etcd_tls_config` are consistent with the actual mount paths in the Pod.

2. Confirm that `subjectAltName` in `server.cnf` covers `etcd_config.etcd_host` and the domain names of all ETCD nodes.

3. Confirm that `--client-cert-auth` is enabled on the ETCD server side. Otherwise, client certificates do not take effect.

### Standby Pod Stays Pending, or Active and Standby Pods Are Not Scheduled to Different Nodes

**Symptom**

After the active/standby switchover is enabled, only one Pod is started, and the other stays Pending for a long time.

**Cause Analysis**

The deployment template enables mandatory anti-affinity scheduling by default (`podAntiAffinity`, `topologyKey: kubernetes.io/hostname`), so the two Pods of the same component must be scheduled to different nodes. If the number of schedulable nodes in the cluster is insufficient, node resources are insufficient, or nodes have taints, the second Pod stays Pending because the anti-affinity constraint cannot be satisfied.

**Solution**

Confirm that the cluster has at least two nodes that meet the resource and scheduling constraints, or adjust the anti-affinity configuration in the template based on actual disaster recovery requirements.

### Inference Requests Return 503 or Connection Fails During Active/Standby Switchover

**Symptom**

After the active node fails, inference requests return 503 or fail to connect for a short period (from several seconds to tens of seconds), and then recover automatically.

**Cause Analysis**

The switchover process consists of several phases: "the original active node loses the ETCD lease -> the standby node acquires the lock and becomes active -> the new active node restores READY and is added to the Service Endpoint". During this period, the Service may have no available ready endpoint. This takes about 10 seconds for the Coordinator and about 20 seconds for the Controller.

**Solution**

This is expected behavior, and request retries must be configured on the service side. Do not rely on "zero service interruption" for a long time. To shorten the switchover time, you can reduce `master_lock_ttl` appropriately (the Coordinator side automatically limits it to no more than 8 seconds).

### Changes to enable_master_standby Do Not Take Effect

**Symptom**

After `enable_master_standby` is modified in `user_config.json`, the active/standby mode of the running service does not change.

**Cause Analysis**

The active/standby switch is read when the process starts, and dynamic switching at runtime is not supported. In addition, the number of Pod replicas (`replicas`) is generated during deployment based on this switch, and modifying the configuration at runtime does not change the number of replicas.

**Solution**

After modifying the configuration, redeploy the service (regenerate and apply the yaml so that the Pods are recreated). Do not expect the changes to take effect dynamically at runtime.
