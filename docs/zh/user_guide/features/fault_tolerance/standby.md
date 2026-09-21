# 主备倒换特性

## 特性介绍

主备倒换特性主要通过ETCD分布式锁实现，确保系统高可用性，包括Controller主备和Coordinator主备。开启主备倒换特性开关后，系统会在初始化阶段拉起两个实例，通过ETCD分布式锁竞争来实现主备身份选举，当主节点发生故障时，备用节点能在设定时间间隔后自动接管工作。

开启主备倒换特性后，Controller和Coordinator会各拉起2个Pod，主节点对外提供服务，备节点不承接业务流量，仅参与选主。Controller和Coordinator使用相互独立的ETCD锁键，两组件的选举互不影响，可以只开启其中一个，也可以同时开启。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | 不依赖特定硬件型号。配合故障感知等能力使用时，建议在`motor_deploy_config`中显式配置`hardware_type`。 |
| 部署场景 | <ul><li>主、备节点（Controller/Coordinator）不建议部署在同一台节点上；部署模板已默认配置强制反亲和调度（`podAntiAffinity`，`topologyKey: kubernetes.io/hostname`），主备Pod会自动分散到不同节点。</li><li>开启主备后，部署工具会自动将Controller/Coordinator的Pod副本数（`replicas`）置为2，无需手工扩副本。</li></ul> |
| 引擎 | 不涉及，特性本身不依赖特定推理引擎，vLLM、SGLang等引擎均可使用。 |
| 特性互斥 | 无显式互斥说明，可与其他特性共存。 |
| 软件依赖 | ETCD服务端需要使用v3.6版本。 |
| 其他限制 | <ul><li>特性生效依赖ETCD服务端正确部署，服务端至少需要3个副本，以保证ETCD集群的可靠性；</li><li>Coordinator、Controller主备倒换特性可以共用一套ETCD；多套大EP集群可以共用一套ETCD，通过命名空间区分；</li><li>开启主备后系统会自动开启ETCD持久化（`etcd_config.enable_etcd_persistence`），备节点依赖ETCD中的持久化数据在倒换后恢复业务状态；</li><li>主备倒换需要等待ETCD租约超时，倒换期间存在秒级的服务不可用窗口，业务侧需具备重试能力。</li></ul> |

## 特性使用

### 环境准备

主备倒换依赖ETCD分布式锁功能，涉及集群内不同POD间通信，建议使用CA证书做双向认证。ETCD集群部署包含两部分：生成ETCD安全证书和部署ETCD服务端。ETCD服务端仅需部署一套，Coordinator、Controller主备倒换特性可以共用一套ETCD。

#### 生成ETCD安全证书（可选）

>[!NOTE] 说明
>如果不使用CA证书做双向认证加密通信，则服务间将进行明文传输，可能会存在较高的网络安全风险。

1. 请用户自行准备证书生成的相关前置文件，文件放置目录以 /home/{用户名}/auto_gen_ms_cert为例。

    **server.cnf**

    ```txt
    [req] # 主要请求内容
    req_extensions = v3_req
    distinguished_name = req_distinguished_name

    [req_distinguished_name] # 证书主体信息
    countryName = CN
    stateOrProvinceName = State
    localityName = City
    organizationName = Organization
    organizationalUnitName = Unit
    commonName = etcd-server

    [v3_req] # 核心属性
    basicConstraints = CA:FALSE
    keyUsage = digitalSignature, keyEncipherment
    extendedKeyUsage = serverAuth, clientAuth
    subjectAltName = @alt_names

    [alt_names] # 服务标识
    DNS.1 = etcd
    DNS.2 = etcd.default
    DNS.3 = etcd.default.svc
    DNS.4 = etcd.default.svc.cluster.local  #ETCD需部署在default命名空间
    DNS.5 = etcd-0.etcd
    DNS.6 = etcd-0.etcd.default.svc.cluster.local
    DNS.7 = etcd-1.etcd
    DNS.8 = etcd-1.etcd.default.svc.cluster.local
    DNS.9 = etcd-2.etcd
    DNS.10 = etcd-2.etcd.default.svc.cluster.local
    ```

    >[!NOTE] 说明
    >`alt_names`中的域名必须覆盖Controller/Coordinator配置的`etcd_config.etcd_host`与ETCD集群各节点域名，否则开启TLS后客户端会因证书校验失败而无法连接ETCD。

    **client.cnf**

    ```txt
    [req] # 主要请求内容
    req_extensions = v3_req
    distinguished_name = req_distinguished_name

    [req_distinguished_name] # 证书主体信息
    countryName = CN
    stateOrProvinceName = State
    localityName = City
    organizationName = Organization
    organizationalUnitName = Unit
    commonName = etcd-client

    [v3_req] # 核心属性
    basicConstraints = CA:FALSE
    keyUsage = digitalSignature, keyEncipherment
    extendedKeyUsage = clientAuth
    subjectAltName = @alt_names

    [alt_names] # 服务标识
    DNS.1 = mindie-service-controller
    DNS.2 = mindie-service-coordinator

    ```

    **crl.conf**

    ```txt
    # OpenSSL configuration for CRL generation
    #
    ####################################################################
    [ ca ] # CA框架声明，指示OpenSSL使用哪个预定义的CA配置块作为默认设置
    default_ca = CA_default # The default ca section
    ####################################################################
    [ CA_default ] # 核心CA设置，所有关键路径、文件和默认操作
    dir             = {dir}  # 添加此根目录定义,如/home/{用户名}/auto_gen_ms_cert
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
    countryName             = optional  # C：可选
    stateOrProvinceName     = optional  # ST：可选
    localityName            = optional  # L（城市）：可选
    organizationName        = optional  # O：可选
    organizationalUnitName  = optional  # OU：可选
    commonName              = supplied  # CN：必须提供
    emailAddress            = optional  # Email：可选
    ####################################################################
    [ crl_ext ] # CRL扩展属性
    # CRL extensions.
    # Only issuerAltName and authorityKeyIdentifier make any sense in a CRL.
    # issuerAltName=issuer:copy
    authorityKeyIdentifier=keyid:always,issuer:always
    ```

    >[!NOTE] 说明
    >文件中{dir}路径建议为各节点都能访问的共享目录。

    **gen_etcd_controller_ca.sh**

    ```bash
    #!/bin/bash
    # 配置基本目录，与crl.conf对应
    base_dir=/home/{用户名}/auto_gen_ms_cert
    # 1. 创建所需文件和目录
    mkdir -p ${base_dir}/etcd_crl/newcerts
    touch ${base_dir}/etcd_crl/index.txt
    echo 1000 > ${base_dir}/etcd_crl/pulp_crl_number
    echo "01" > ${base_dir}/etcd_crl/serial
    # 2. 设置权限
    chmod 700 ${base_dir}/etcd_crl/newcerts
    chmod 600 ${base_dir}/etcd_crl/{index.txt,pulp_crl_number,serial}
    # 3. 创建CA证书
    openssl genrsa -aes256 -out ca.key 4096
    openssl req -x509 -new -nodes -key ca.key \
    -subj "/CN=my-cluster-ca" \
    -days 3650 -out ca.pem
    # 4. 生成服务端证书
    openssl genrsa -out server.key 4096
    openssl req -new -key server.key -out server.csr \
    -subj "/CN=etcd-server" -config server.cnf
    openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out server.pem -days 3650 -extensions v3_req -extfile server.cnf
    # 5. 生成客户端证书
    openssl genrsa -out client.key 4096
    openssl req -new -key client.key -out client.csr \
    -subj "/CN=inst0-client" -config client.cnf
    openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out client.pem -days 3650 -extensions v3_req -extfile client.cnf
    # 6. 设置权限
    chmod 0400 ./*.key
    chmod 0400 ./*.pem
    ```

    >[!NOTE] 说明
    > 生成ca.key时的密码需要牢记，生成新证书时需要用到。

2. 执行以下命令运行gen_etcd_controller_ca.sh，生成服务端证书、客户端证书等文件。

    ```bash
    bash gen_etcd_controller_ca.sh
    ```

    回显类似如下内容表示生成成功：

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

    运行完成后，在当前目录生成以下文件或目录：

    ```txt
    ca.key
    ca.pem
    ca.srl
    client.cnf
    client.csr
    client.key
    client.pem
    crl.conf
    etcd_crl   # crl相关文件夹
    gen_etcd_controller_ca.sh
    server.cnf
    server.csr
    server.key
    server.pem
    ```

    通过以上的操作，生成了CA证书、Server端证书和一份Client端证书。（CA证书用于认证，Server证书用于ETCD集群部署，Client证书用于Controller/Coordinator主备份）

    >[!NOTE] 说明
    > 如果需要多份Client端证书，使用同一CA证书，重复执行以下操作：
    >
    > ```bash
    > openssl genrsa -out {新client}.key 4096
    > openssl req -new -key {新client}.key -out {新client}.csr \
    > -subj "/CN={新client的CN}" -config client.cnf
    > openssl x509 -req -in {新client}.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    > -out {新client}.pem -days 3650 -extensions v3_req -extfile client.cnf
    > # 修改新生成证书的权限
    > chmod 0400 ./*.key
    > chmod 0400 ./*.pem
    >  ```

#### 部署ETCD服务端

部署参考样例如下:

1. 执行以下命令加载ETCD镜像。

    ```bash
    docker pull quay.io/coreos/etcd:v3.6.0-rc.4
    ```

    >[!NOTE] 说明
    >如果docker pull失败，可以用podman命令下载ETCD镜像后保存，再使用docker load命令导入，命令如下：
    >
    > ```bash
    > apt install podman
    > podman pull quay.io/coreos/etcd:v3.6.0-rc.4
    > ```
    >
    > ETCD至少需要三副本部署，在指定的节点上导入此镜像。

2. 在集群中创建ETCD资源。

    1. 执行以下命令自行创建local-pvs.yaml文件。

        ```bash
        vim local-pvs.yaml
        ```

        在文件中写入以下内容：

        ```yaml
        # local-pvs.yaml 创建PV
        apiVersion: v1
        kind: PersistentVolume
        metadata:
          name: etcd-data-0  # 必须与StatefulSet的PVC命名规则匹配
        spec:
          capacity:
            storage: 4096M
          volumeMode: Filesystem
          accessModes: [ReadWriteOnce]
          persistentVolumeReclaimPolicy: Retain
          storageClassName: local-storage  # 必须与PVC的storageClass匹配
          local:
            path: /mnt/data/etcd-0  # 节点上的实际路径
          nodeAffinity:
            required:
              nodeSelectorTerms:
                - matchExpressions:
                    - key: kubernetes.io/hostname
                      operator: In
                      values: ["ubuntu"]  # 绑定到特定节点，即NodeName

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
                      values: ["worker-80-39"] # 绑定到特定节点，即NodeName

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
                      values: ["worker-153"] # 绑定到特定节点，即NodeName
        ```

        关键参数如下所示：
        - `spec.local.path`：对应节点的路径，必须真实且存在。
        - `spec.nodeAffinity.required.nodeSelectorTerms.matchExpressions.values`：待部署的节点名称。

    2. 在K8s集群的master节点执行以下命令创建pvs。

        ```bash
        kubectl apply -f local-pvs.yaml
        ```

        返回结果如下所示表示创建成功：

        ```bash
        persistentvolume/etcd-data-0 created
        persistentvolume/etcd-data-1 created
        persistentvolume/etcd-data-2 created
        ```

    3. 执行以下命令在3个节点上打上app=etcd标签。

        ```bash
        kubectl label nodes <节点名> app=etcd
        ```

        返回结果如下所示表示创建成功：

        ```bash
        node/<节点名> labeled
        ```

    4. 执行以下命令自行创建etcd.yaml文件，配置ETCD Pod侧证书。

        ```bash
        vim etcd.yaml
        ```

        根据[生成ETCD安全证书](#生成etcd安全证书可选)生成的证书，将文件生成路径挂载至ETCD容器内，并配置ETCD使用加密通信，指定使用ca.pem、server.pem和server.key进行通信。

        ```yaml
        # etcd.yaml 在3个节点上创建同步的ETCD数据库
        ---
        apiVersion: v1
        kind: Service
        metadata:
          name: etcd
          namespace: default
        spec:
          type: ClusterIP
          clusterIP: None # Headless Service，用于StatefulSet的DNS解析
          selector:
            app: etcd  # 选择标签为app=etcd的Pod
          publishNotReadyAddresses: true  # 允许未就绪Pod被DNS发现
          ports:
            - name: etcd-client
              port: 2379 # 客户端通信端口
            - name: etcd-server
              port: 2380 # 节点间通信端口
            - name: etcd-metrics
              port: 8080 # ETCD 集群管控端口
        ---
        apiVersion: apps/v1
        kind: StatefulSet
        metadata:
          name: etcd
          namespace: default
        spec:
          serviceName: etcd # 绑定 Headless Service
          replicas: 3 # 奇数节点保证Raft
          podManagementPolicy: OrderedReady # 允许并行启动（需配合初始化脚本）
          updateStrategy:
            type: RollingUpdate # 滚动更新策略
          selector:
            matchLabels:
              app: etcd # 匹配 Pod 标签
          template:
            metadata:
              labels:
                app: etcd # Pod 标签
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
                      topologyKey: "kubernetes.io/hostname" # 跨节点部署
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
                    - --name=$(HOSTNAME) # 节点唯一标识
                    - --data-dir=/data # 数据存储路径
                    - --wal-dir=/data/wal
                    - --listen-peer-urls=https://0.0.0.0:2380 # 监听节点间通信
                    - --listen-client-urls=https://0.0.0.0:2379 # 监听客户端请求
                    - --advertise-client-urls=https://$(HOSTNAME).$(SERVICE_NAME):2379  # 客户端地址
                    - --initial-cluster-state=new # 新集群初始化模式
                    - --initial-cluster-token=etcd-$(K8S_NAMESPACE) # 集群唯一标识
                    - --initial-cluster=etcd-0=https://etcd-0.etcd:2380,etcd-1=https://etcd-1.etcd:2380,etcd-2=https://etcd-2.etcd:2380 # 初始节点列表
                    - --initial-advertise-peer-urls=https://$(HOSTNAME).$(SERVICE_NAME):2380 # 对外公布的节点间通信地址
                    - --listen-metrics-urls=http://0.0.0.0:8080 # 集群管控地址
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
                      mountPath: /data # 挂载持久化存储
                    - name: etcd-ca
                      mountPath: /etc/ssl/certs/etcdca # 物理机/home/{用户名}/auto_gen_ms_cert目录在容器中的挂载路径
              volumes:
                - name: etcd-ca
                  hostPath:
                    path: /home/{用户名}/auto_gen_ms_cert # 物理机创建文件及生成文件路径
                    type: Directory
          volumeClaimTemplates:
            - metadata:
                name: etcd-data
              spec:
                accessModes: [ "ReadWriteOnce" ] # 单节点读写
                storageClassName: local-storage
                resources:
                  requests:
                    storage: 4096M # 存储空间
        ```

        关键参数如下所示：

        - `spec.template.spec.containers.args.--client-cert-auth`： 启用客户端证书认证
        - `spec.template.spec.containers.args.--cert-file`：指定服务端证书
        - `spec.template.spec.containers.args.--key-file`：指定服务端私钥
        - `spec.template.spec.containers.args.--trusted-ca-file`：指定信任的CA根证书
        - `spec.template.spec.containers.args.--peer-client-cert-auth`：启用peer节点间的客户端证书认证
        - `spec.template.spec.containers.args.--peer-trusted-ca-file`：指定信任的CA根证书（用于peer）
        - `spec.template.spec.containers.args.--peer-cert-file`：指定本节点作为peer的证书
        - `spec.template.spec.containers.args.--peer-key-file`：指定本节点作为peer的私钥

        >[!NOTE] 说明
        >样例中`--client-cert-auth`、`--peer-client-cert-auth`等TLS参数仅在[生成ETCD安全证书](#生成etcd安全证书可选)已执行时需要保留；若不使用CA证书，请删除上述TLS相关参数与`etcd-ca`卷，否则ETCD会因找不到证书文件而启动失败。

    5. 在K8s集群master节点执行如下命令部署ETCD服务端。

        ```bash
        kubectl apply -f etcd.yaml
        ```

        返回结果如下所示表示创建成功：

        ```txt
        service/etcd created
        statefulset.apps/etcd created
        ```

    6. 执行以下命令查询ETCD集群的Pod。

        ```bash
        kubectl get pod -A
        ```

        回显如下所示：

        ```txt
        NAMESPACE       NAME    READY   STATUS   RESTARTS    AGE IP               NODE          NOMINATED NODE   READINESS GATES
        default         etcd-0  1/1     Running  0           44h xxx.xxx.xxx.xxx  ubuntu        <none>           <none>
        default         etcd-1  1/1     Running  0           44h xxx.xxx.xxx.xxx  worker-153    <none>           <none>
        default         etcd-2  1/1     Running  0           44h xxx.xxx.xxx.xxx  worker-80-39  <none>           <none>
        ```

        >[!NOTE] 说明
        >- 修改etcd.yaml后采用哪种生效方式，取决于修改的字段：
        >
        >   - 仅修改image、env、args等，可通过滚动更新生效的字段，StatefulSet为RollingUpdate策略，会按序滚动启动各Pod，无需删除PVC。执行命令如下所示。
        >
        >      ```bash
        >      kubectl -n <namespace> apply -f etcd.yaml
        >      ```
        >
        >   - 修改 --name、--initial-cluster、--data-dir、replicas、storageClassName、nodeAffinity、local.path 等结构性字段时，需要重建ETCD集群，操作步骤如下。
        >
        >      ```bash
        >      # 1. 删除Service和StatefulSet（PVC不会同步被删除）
        >      kubectl -n <namespace> delete -f etcd.yaml
        >      # 2. 精确删除本ETCD的3个PVC（命名规则：<volumeClaimTemplate名>-<StatefulSet名>-<序号>）
        >      kubectl -n <namespace> delete pvc etcd-data-etcd-0 etcd-data-etcd-1 etcd-data-etcd-2
        >      # 3. 删除对应的PV（PV为Retain策略，删除PVC后处于Released状态，此时方可删除）
        >      kubectl -n <namespace> delete pv etcd-data-0 etcd-data-1 etcd-data-2
        >      ```
        >
        >- 请勿使用 `kubectl delete pvc --all` 命令，该命令会删除当前命名空间下所有PVC，可能误删其他业务使用的存储卷（例如KV池化等特性使用的 `mindie-motor-store-*`），造成不可恢复的数据丢失。
        >- 如需备份数据，可在删除前执行以下命令（TLS参数请按实际证书路径调整）：
        >
        >   ```bash
        >   kubectl -n <namespace> exec etcd-0 --etcdctl --endpoints=https://etcd-0.etcd:2379 --cacert=/etc/ssl/certs/etcdca/ca.pem --cert=/etc/ssl/certs/etcdca/server.pem --key=/etc/ssl/certs/etcdca/server.key snapshot save /tmp/etcd-backup.db
        >   ```
        >
        >   备份文件位于容器内 /tmp/etcd-backup.db ，可执行以下命令导出到本地。
        >
        >   ```bash
        >   kubectl -n <namespace> cp <etcd-pod>:/tmp/etcd-backup.db ./etcd-backup.db
        >   ```
        >
        >- 再删除etcd-0，etcd-1，etcd-2数据库中内容（删除前请确认已完成备份，且该目录仅用于本ETCD集群）:
        >
        >    ```bash
        >    rm -rf /mnt/data/etcd-0/*
        >    rm -rf /mnt/data/etcd-1/*
        >    rm -rf /mnt/data/etcd-2/*
        >    ```

#### 配置K8s管理端（可选）

当硬件出现故障时（如机器重启），K8s集群无法迅速感知容器Pod的状态，导致推理业务无法在指定时间内恢复，可通过执行如下步骤以加快业务恢复速度。

>[!NOTE] 说明
>如果不要求硬件故障影响时长，可不执行下述步骤。

1. 执行以下命令查询K8s管理节点心跳超时标记阈值（`node-monitor-grace-period`），如果结果为空表示为默认值。

    ```bash
    kubectl describe pod <kube-controller-manager-pod 名> -n kube-system | grep "node-monitor-grace-period"
    ```

2. 执行以下命令打开并修改配置节点心跳超时标记阈值（`node-monitor-grace-period`），配置文件所在路径一般存放在控制平面节点（运行kube-controller-manager的节点）的/etc/kubernetes/manifests/kube-controller-manager.yaml目录。

    ```bash
    vi /etc/kubernetes/manifests/kube-controller-manager.yaml
    ```

      修改内容如下所示（该文件为静态Pod清单，请仅追加/修改下述参数行，不要整份替换）：

      ```yaml
      # 以下仅为片段，metadata、name、namespace、spec 等字段在实际路径文件中均存在，请保持原有缩进与内容
      spec:
        containers:
        - command:
            - kube-controller-manager
            # 以上原有参数保持不变
            # 添加/修改 node-monitor-grace-period 参数（改为 20s）
            - --node-monitor-grace-period=20s
            # 以下原有参数保持不变
            ...
      ```

    >[!NOTE] 说明
    >- 修改前建议先备份原文件： cp /etc/kubernetes/manifests/kube-controller-manager.yaml /root/kube-controller-manager.yaml.bak。
    >- 该文件由kubelet监听并据此重建Pod，metadata、name、namespace、spec的缩进层级错误会导致kube-controller-manager无法启动。

3. 按 Esc 键，输入`:wq!`，按 Enter 保存并退出编辑。

4. 执行以下命令重启kube-controller-manager所在节点的K8s服务，从而重启kube-controller-manager服务。

    ```bash
    systemctl restart kubelet.service
    ```

5. 执行以下命令验证参数是否生效。

    ```bash
    kubectl describe pod <kube-controller-manager-pod 名> -n kube-system | grep "node-monitor-grace-period"
    ```

    打印以下内容则表示参数已生效：

    ```txt
    --node-monitor-grace-period=20s
    ```

### 使用场景

主备倒换特性适用于对控制面可用性有要求的场景，Controller主备和Coordinator主备相互独立，可以只开启其中一个，也可以同时开启：

- 场景一：Controller主备倒换。Controller负责实例管理、故障感知与实例分发，单实例故障会导致无法感知推理实例状态、无法向Coordinator分发实例，进而使推理服务不可用。开启主备后，主Controller故障时备用Controller在设定时间间隔后自动接管。
- 场景二：Coordinator主备倒换。Coordinator承载推理入口，单实例故障会直接导致推理请求失败。开启主备后，主Coordinator故障时备用Coordinator在设定时间间隔后自动接管，推理流量随之切换到新的主节点。

### 使用样例

#### 场景一：Controller主备倒换

1. 配置Controller侧证书挂载。（如果不开启CA证书，请跳过此步骤。）

    如果需要开启证书CA认证，根据[生成ETCD安全证书](#生成etcd安全证书可选)生成的相关证书文件，将证书文件的生成路径挂载至Controller容器内。请先根据 motor_deploy_config.deploy_mode 确认待修改的模板文件（默认模式为 infer_service_set，与本文档示例命令一致）：

    | deploy_mode | 待修改文件 | 修改位置 |
    |---|---|---|
    |infer_service_set（默认）|examples/deployer/yaml_template/infer_service_template.yaml（CRD 场景）|roles下name: controller的spec.template.spec|
    |multi_deployment|examples/deployer/yaml_template/controller_template.yaml（multi_deployment 场景）|Deployment中的spec.template.spec|

    以deploy_mode为infer_service_set模式为例，在examples/deployer/yaml_template/infer_service_template.yaml文件中的volumeMounts和volumes中添加以下内容（controller-ca为挂载的证书目录）：

    ```yaml
    ...
              volumeMounts:
              ...
              - name: controller-ca
                mountPath: /usr/local/Ascend/pyMotor/conf/security/etcd # 物理机/home/{用户名}/auto_gen_ms_cert目录在容器中的挂载路径
            volumes:
            ...
            - name: controller-ca
              hostPath:
                path: /home/{用户名}/auto_gen_ms_cert # 物理机创建文件及生成文件路径
                type: Directory
    ...
    ```

    >[!NOTE] 说明
    >- 挂载点`/usr/local/Ascend/pyMotor/conf/security/etcd`是本文档约定的路径，代码中并无该默认值，必须与第2步`etcd_tls_config`中配置的证书路径保持一致。
    >- deploy完成后，可通过以下命令确认证书目录已挂载至生成的yaml中（infer_service_set 模式对应文件路径为：examples/deployer/output_yamls/infer_service.yaml；multi_deployment 模式对应文件路径为：examples/deployer/output_yamls/mindie_motor_controller.yaml，避免模板文件改错导致证书未挂载）
    >
    >   ```bash
    >   grep -A3 controller-ca examples/deployer/output_yamls/infer_service.yaml
    >   ```

2. 配置user_config.json配置文件，开启TLS认证。（如果不开启CA证书，请跳过此步骤。）

    开启CA证书认证：
    - 设置tls_config/etcd_tls_config的`enable_tls`为true；
    - 设置`ca_file`/`cert_file`/`key_file`为对应的文件路径。

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

    >[!NOTE] 说明
    >Controller/Coordinator与ETCD之间使用gRPC双向认证，实际生效的字段只有`enable_tls`、`ca_file`、`cert_file`、`key_file`四个；`passwd_file`与`crl_file`为预留字段，ETCD场景下不生效，可保持默认值。

3. 在user_config.json配置文件中开启Controller主备倒换特性，配置参数如下所示。`enable_master_standby`修改为true。

    ```json
    ...
       "motor_controller_config": {
          "standby_config": {
             "enable_master_standby": true
          }
       }
    ...
    ```

    - false：关闭主备；
    - true：开启主备。
    >[!NOTE] 说明
    >默认使用default工作空间下的ETCD服务端，端口号默认为2379。如果需要修改，在motor_controller_config中修改ETCD信息。域名通常为etcd.{namespace}.svc.cluster.local。
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
    >- 开启主备后系统会自动开启ETCD持久化（`etcd_config.enable_etcd_persistence`置为`true`），用户无需手动配置。
    >- `standby_config`的其余参数（`master_standby_check_interval`默认5秒、`master_lock_ttl`默认15秒等）一般保持默认即可，增大`master_lock_ttl`会延长倒换耗时。

4. 在 examples/deployer 目录下执行以下命令启动，支持指定配置目录或单独指定配置文件。

    ```bash
    cd examples/deployer
    # 方式一：指定配置目录（推荐）
    python deploy.py --config_dir ../infer_engines/vllm

    # 方式二：单独指定配置文件
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    部署完成后Controller的Pod副本数为2。

5. 发送请求验证服务是否启动成功。

    >[!NOTE] 说明
    > Controller仅提供管理面接口（`motor_controller_config.api_config.controller_api_port`，默认为：1026），不提供/v1/chat/completions 等推理接口，推理请求需发送至Coordinator主节点。Controller的主备状态请通过[验证特性](#验证特性)中的日志和Pod READY状态确认。

    有以下两种方式发送请求：

    - Coordinator主节点的PodIP和端口号：`http://PodIP:1025`。（其中仅有READY为1/1的Coordinator才可执行推理请求）
    - K8s集群内任意物理机IP:31015（端口号需与 examples/deployer/yaml_template/coordinator_template.yaml（multi_deployment 场景）或 examples/deployer/yaml_template/infer_service_template.yaml（CRD 场景）中 mindie-motor-coordinator-infer 的 nodePort 端口保持一致）。

    使用物理机IP和端口号方式样例：

    ```bash
    #!/bin/bash
    url="http://{物理机IP地址}:31015/v1/chat/completions"
    data='{
        "model": "deepseek",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "你是谁"}]
    }'
    curl  $url -X POST  -d "$data"
    ```

    回显如下，表示服务启动成功：

    ```txt
    ...
       "message": {
          "role": "assistant",
          "content": "🤔\n好的，用户问我是谁。我",
       ...
       }
    ...
    ```

#### 场景二：Coordinator主备倒换

1. 配置Coordinator侧证书挂载。（如果不开启CA证书，请跳过此步骤。）

    如果需要开启证书CA认证，根据[生成ETCD安全证书](#生成etcd安全证书可选)生成的相关证书文件，将证书文件的生成路径挂载至Coordinator容器内。请先根据 motor_deploy_config.deploy_mode 确认待修改的模板文件（默认模式为 infer_service_set，与本文档示例命令一致）：

    | deploy_mode | 待修改文件 | 修改位置 |
    |---|---|---|
    |infer_service_set（默认）|examples/deployer/yaml_template/infer_service_template.yaml（CRD 场景）|roles下name: coordinator的spec.template.spec|
    |multi_deployment|examples/deployer/yaml_template/coordinator_template.yaml（multi_deployment 场景）|Deployment中的spec.template.spec|

    以deploy_mode为multi_deployment模式为例，在examples/deployer/yaml_template/coordinator_template.yaml文件中的volumeMounts和volumes中添加以下内容（coordinator-ca为挂载的证书目录）：

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
            mountPath: /usr/local/Ascend/pyMotor/conf/security/etcd # 物理机/home/{用户名}/auto_gen_ms_cert目录在容器中的挂载路径
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
          path: /home/{用户名}/auto_gen_ms_cert # 物理机创建文件及生成文件路径
          type: Directory
    ...
    ```

    >[!NOTE] 说明
    >- 挂载点`/usr/local/Ascend/pyMotor/conf/security/etcd`是本文档约定的路径，代码中并无该默认值，必须与第2步`etcd_tls_config`中配置的证书路径保持一致。
    >- deploy完成后，可通过以下命令确认证书目录已挂载至生成的yaml中（multi_deployment 模式对应文件路径为：examples/deployer/output_yamls/mindie_motor_coordinator.yaml；infer_service_set 模式对应文件路径为：examples/deployer/output_yamls/infer_service.yaml，避免模板文件改错导致证书未挂载）
    >
    >   ```bash
    >   grep -A3 coordinator-ca examples/deployer/output_yamls/mindie_motor_coordinator.yaml
    >   ```

2. 配置user_config.json配置文件，开启TLS认证。（如果不开启CA证书，请跳过此步骤。）

    开启CA证书认证：
    - 设置tls_config/etcd_tls_config的`enable_tls`为true；
    - 设置`ca_file`/`cert_file`/`key_file`为对应的文件路径。

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

    >[!NOTE] 说明
    >Controller/Coordinator与ETCD之间使用gRPC双向认证，实际生效的字段只有`enable_tls`、`ca_file`、`cert_file`、`key_file`四个；`passwd_file`与`crl_file`为预留字段，ETCD场景下不生效，可保持默认值。

3. 在user_config.json配置文件中开启Coordinator主备倒换特性，配置参数如下所示。`enable_master_standby`修改为true。

    ```json
    ...
       "motor_coordinator_config": {
          "standby_config": {
             "enable_master_standby": true
          }
       }
    ...
    ```

    - false：关闭主备；
    - true：开启主备。
    >[!NOTE] 说明
    >默认使用default工作空间下的ETCD服务端，端口号默认为2379。如果需要修改，在motor_coordinator_config中修改ETCD信息。域名通常为etcd.{namespace}.svc.cluster.local。
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
    >- 开启主备后系统会自动开启ETCD持久化（`etcd_config.enable_etcd_persistence`置为`true`），用户无需手动配置。
    >- Coordinator运行时会自动将`master_lock_ttl`限制为不大于8秒、`master_standby_check_interval`限制为不大于2秒，以保证倒换能够在30秒内完成，配置更大值不会生效。

4. 在 examples/deployer 目录下执行以下命令启动，支持指定配置目录或单独指定配置文件。

    ```bash
    cd examples/deployer
    # 方式一：指定配置目录（推荐）
    python deploy.py --config_dir ../infer_engines/vllm

    # 方式二：单独指定配置文件
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    部署完成后Coordinator的Pod副本数为2。

5. 发送请求验证服务是否启动成功。

    有以下两种方式发送请求：
    - Coordinator主节点的PodIP和端口号：http://PodIP:1025。（其中仅有READY为1/1的才可执行推理请求）
    - K8s集群内任意物理机IP:31015（端口号需与 examples/deployer/yaml_template/coordinator_template.yaml（multi_deployment 场景）或 examples/deployer/yaml_template/infer_service_template.yaml（CRD 场景）中 mindie-motor-coordinator-infer 的 nodePort 端口保持一致）。

    该样例使用物理机IP和端口号方式：

    ```bash
    #!/bin/bash
    url="http://{物理机IP地址}:31015/v1/chat/completions"
    data='{
        "model": "deepseek",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "你是谁"}]
    }'
    curl  $url -X POST  -d "$data"
    ```

    回显如下，表示服务启动成功：

    ```json
    ...
       "message": {
          "role": "assistant",
          "content": "🤔\n好的，用户问我是谁。我",
       ...
       }
    ...
    ```

### 验证特性

服务启动后，可通过以下方式验证主备倒换特性是否生效。开启主备后，Controller、Coordinator各自有2个Pod，其中**有且仅有一个Pod的READY为1/1**（主节点），另一个为0/1（备节点）。

**方式一：检查主备角色（日志方式）**

查询对应节点日志，如果日志中出现"Role changed from standby to master"，表明当前节点抢到ETCD分布式锁，为主节点。

```bash
kubectl -n <namespace> logs <pod名> | grep "Role changed"
```

**方式二：检查主备角色（K8s命令方式）**

```bash
kubectl -n <namespace> get pod -owide
```

有且仅有一个Controller/Coordinator pod READY状态为1/1，表示该节点为主节点；READY为0/1的为备节点，属预期现象。

**方式三：检查主备角色（接口方式，可选）**

Controller使用管理面端口（`motor_controller_config.api_config.controller_api_port`，默认1026），Coordinator使用管控面端口（`motor_coordinator_config.api_config.coordinator_api_mgmt_port`，默认1026），访问`/readiness`：

```bash
# Controller
curl http://{Controller PodIP}:1026/readiness
# Coordinator
curl http://{Coordinator PodIP}:1026/readiness
```

- Controller主节点返回200：`{"message": "Controller is ready"}`；备节点返回503：`{"detail": {"message": "Controller is not ready", "reason": "Not master"}}`。
- Coordinator主节点返回200：`{"status": "ok", "message": "Coordinator is master", "ready": true}`；备节点返回503：`{"detail": "Coordinator is not master"}`。

**方式四：倒换演练（可选）**

删除主节点Pod，模拟主节点故障：

```bash
kubectl -n <namespace> delete pod <主节点Pod名>
```

预期结果：备节点在ETCD租约超时后抢锁成功，日志出现"Role changed from standby to master"，READY变为1/1，推理请求由新的主节点承接。Coordinator升主约需10秒（租约TTL 8秒 + 探测间隔2秒），Controller约需20秒（租约TTL 15秒 + 探测间隔5秒），期间推理请求可能短暂失败，请确认业务侧已配置重试。

## 常见问题

### 备节点的READY一直为0/1，是否正常

**问题描述**

开启主备后，Controller/Coordinator各有2个Pod，其中一个长期READY为0/1。

**原因分析**

主备模式下只有主节点对外提供服务。备节点的`/readiness`返回503（Coordinator返回`Coordinator is not master`，Controller返回`Not master`），K8s据此将备节点标记为未就绪并移出Service Endpoint。备节点进程本身是正常运行的，其`/liveness`仍返回200。

**解决步骤**

属预期行为，无需处理。若两个Pod的READY均为0/1，说明没有节点抢到ETCD分布式锁，请参考下一条排查。

### 两个Pod的READY均为0/1，一直选不出主节点

**问题描述**

Controller/Coordinator的两个Pod长期都是0/1，服务不可用。

**原因分析**

备节点无法抢到ETCD分布式锁，常见原因包括：

- ETCD服务端未部署或不可用；
- `etcd_config.etcd_host`/`etcd_port`配置错误，或域名无法解析；
- 开启了TLS但证书路径、证书内容不正确；
- ETCD集群失去多数派（3副本中超过1个副本故障）。

**解决步骤**

1. 确认ETCD集群状态正常：

    ```bash
    kubectl -n <namespace> get pod | grep etcd
    ```

2. 确认`user_config.json`中的`etcd_config`与ETCD实际部署一致（域名通常为`etcd.{namespace}.svc.cluster.local`，端口2379）。
3. 在Controller/Coordinator容器内确认能够访问ETCD，并检查容器日志中是否有连接ETCD失败或选主失败的记录。

### 开启TLS后Controller/Coordinator无法连接ETCD

**问题描述**

关闭TLS时主备倒换正常，开启TLS后两个Pod都无法升主，容器日志出现证书相关报错。

**原因分析**

- 容器内证书路径与`etcd_tls_config`中配置的路径不一致（部署模板默认不挂载证书目录，需手工添加挂载）；
- `server.cnf`中`alt_names`未包含`etcd_config.etcd_host`使用的域名，证书校验失败；
- 客户端证书`cert_file`/`key_file`不是由同一CA签发。

**解决步骤**

1. 核对`etcd_tls_config`中的`ca_file`/`cert_file`/`key_file`与Pod内实际挂载路径一致。
2. 确认`server.cnf`的`subjectAltName`覆盖了`etcd_config.etcd_host`及ETCD各节点域名。
3. 确认ETCD服务端已启用`--client-cert-auth`，否则客户端证书不会生效。

### 备节点Pod一直Pending，或主备Pod未调度到不同节点

**问题描述**

开启主备后只起来一个Pod，另一个长期Pending。

**原因分析**

部署模板默认开启了强制反亲和调度（`podAntiAffinity`，`topologyKey: kubernetes.io/hostname`），同一组件的两个Pod必须调度到不同节点。集群内可调度节点不足、节点资源不足或节点带有污点时，第2个Pod会因无法满足反亲和约束而Pending。

**解决步骤**

确认集群内至少有2个满足资源与调度约束的节点，或根据实际容灾诉求调整模板中的反亲和配置。

### 主备倒换期间推理请求返回503或连接失败

**问题描述**

主节点故障后，推理请求短时间（数秒到数十秒）返回503或连接失败，之后自动恢复。

**原因分析**

倒换过程包含"原主节点失去ETCD租约→备节点抢锁升主→新主节点恢复READY并被加入Service Endpoint"几个阶段，期间Service可能没有可用的就绪端点。Coordinator约需10秒，Controller约需20秒。

**解决步骤**

属预期行为，业务侧需配置请求重试。请勿长期依赖"服务零中断"，如需缩短倒换时间可适当减小`master_lock_ttl`（Coordinator侧会被自动限制为不大于8秒）。

### 修改enable_master_standby后不生效

**问题描述**

在`user_config.json`中修改`enable_master_standby`后，运行中的服务主备模式没有变化。

**原因分析**

主备开关在进程启动时读取，运行期不支持动态切换；此外Pod副本数（`replicas`）是在部署时根据该开关生成的，运行期修改配置不会改变副本数。

**解决步骤**

修改配置后重新执行部署（重新生成并应用yaml，使Pod重建），不要期望运行期动态生效。
