# 主备倒换特性

主备倒换特性主要通过ETCD分布式锁实现，确保系统高可用性。包括Controller主备和Coordinator主备。

# Controller主备倒换

## 特性介绍

本特性通过ETCD分布式锁机制实现Kubernetes集群中Controller的主备倒换功能，确保系统高可用性。开启Controller主备倒换特性开关后，系统会在初始化阶段拉起两个Controller实例，通过ETCD分布式锁竞争来实现主备身份选举，当主Controller发生故障时，备用Controller能在设定时间间隔后自动接管工作。

**限制与约束**

- 主、备Controller节点不建议部署在同一台节点上。
- ETCD服务端需要使用v3.6版本。
- 特性生效依赖ETCD服务端正确部署，服务端至少需要3个副本，以保证ETCD集群的可靠性。
- Coordinator、Controller主备倒换特性可以共用一套ETCD；多套大EP集群可以共用一套ETCD，通过命名空间区分。

## 部署流程

### 生成ETCD安全证书（可选）

Controller主备倒换依赖ETCD分布式锁功能，涉及集群内不同POD间通信，建议使用CA证书做双向认证，证书配置请参考[证书生成](#生成etcd安全证书可选)。
>[!NOTE]说明
>如果不使用CA证书做双向认证加密通信，则服务间将进行明文传输，可能会存在较高的网络安全风险。

### 部署ETCD服务端

ETCD服务端仅需部署一套，请参考[ETCD部署](#部署etcd服务端)。

### 配置K8s管理端（可选）

当硬件出现故障时（如机器重启），K8s集群无法迅速感知容器Pod的状态，导致推理业务无法在指定时间内恢复，可通过执行如下步骤以加快业务恢复速度。

>[!NOTE]说明
>如果不要求硬件故障影响时长，可不执行下述步骤。

1. 执行以下命令查询K8s管理节点心跳超时标记阈值（node-monitor-grace-period），如果结果为空表示为默认值。

    ```bash
    kubectl describe pod <kube-controller-manager-pod 名> -n kube-system | grep "node-monitor-grace-period"
    ```

2. 执行以下命令打开并修改配置节点心跳超时标记阈值（node-monitor-grace-period），配置文件所在路径一般存放在控制平面节点（运行kube-controller-manager的节点）的/etc/kubernetes/manifests/kube-controller-manager.yaml目录。

    ```bash
    vi /etc/kubernetes/manifests/kube-controller-manager.yaml
    ```

      修改内容如下所示：

      ```yaml
        apiVersion: v1
        kind: Pod
        metadata:
        name: kube-controller-manager-<控制平面节点名>  # 如 kube-controller-manager-node-97-10
        namespace: kube-system
        spec:
        containers:
        - command:
            - kube-controller-manager
            # 其他原有参数...（保留不变）
            - --kubeconfig=/etc/kubernetes/controller-manager.conf
            - --authentication-kubeconfig=/etc/kubernetes/controller-manager.conf
            # 添加/修改 node-monitor-grace-period 参数（改为 20s）
            - --node-monitor-grace-period=20s
            # 其他原有参数...
      ```

3. 按`Esc`键，输入`:wq!`，按`Enter`保存并退出编辑。
4. 执行以下命令重启kube-controller-manager所在节点的K8s服务，从而重启kube-controller-manager服务。

    ```bash
    systemctl restart kubelet.service
    ```

5. 执行以下命令验证参数是否生效。

    ```bash
    kubectl describe pod <kube-controller-manager-pod 名> -n kube-system | grep "node-monitor-grace-period"
    ```

    打印以下内容则表示参数已生效：

    ```bash
    --node-monitor-grace-period=20s
    ```

### 配置Motor

1. 配置Controller侧证书挂载。

    <b>如果不开启CA证书，请跳过此步骤。</b><br>
    如果需要开启证书CA认证，根据[3.1](#生成etcd安全证书可选)生成的相关证书文件，将证书文件的生成路径挂载至Controller容器内。在deployment/controller_init.yaml文件中的volumeMounts和volumes中添加如下内容（controller-ca为挂载的证书目录）：

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

2. 配置user_config.json配置文件，开启TLS认证。

    <b>如果不开启CA证书，请跳过此步骤。</b><br>
    开启CA证书认证：
    - 设置tls_config/etcd_tls_config的"enable_tls"为true；
    - 设置ca_file/cert_file/key_file/passwd_file/tls_crl为对应的文件路径。

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

3. 在user_config.json配置文件中开启Controller主备倒换特性，配置参数如下所示。"enable_master_standby"修改为true。

    ```json
    ...
       "motor_Controller_config": {
          "standby_config": {
             "enable_master_standby": true
          }
       }
    ...
    ```

    - false：关闭主备；
    - true：开启主备。
    >[!NOTE]说明
    > 默认使用default工作空间下的ETCD服务端，端口号默认为2379。如果需要修改，在motor_controller_config中修改ETCD信息。域名通常为`etcd.{namespace}.svc.cluster.local`。
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

### 启动vLLM

1. 在 `examples/deployer` 目录下执行以下命令启动。支持指定配置目录或单独指定配置文件：

    ```bash
    cd examples/deployer
    # 方式一：指定配置目录（推荐）
    python deploy.py --config_dir ../infer_engines/vllm

    # 方式二：单独指定配置文件
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    >[!NOTE]说明
    > * 可通过查询对应节点日志判断Controller主备节点，如果日志中出现"Role changed from standby to master"，表明当前节点抢到ETCD分布式锁，为主节点。<br>
    > * 可通过K8s命令"kubectl get pod -A -owide"查看pod列表，有且仅有一个Controller pod READY状态为1/1，表示该节点为Controller主节点。

2. 发送请求验证服务是否启动成功。

    有以下两种方式发送请求：

    - Controller主节点的虚拟IP和端口号：`http://PodIP:1025`。（其中仅有READY为1/1的才可执行推理请求）
    - K8s集群内任意物理机IP:31015（端口号需与 `examples/deployer/yaml_template/coordinator_template.yaml`（multi_deployment 场景）或 `examples/deployer/yaml_template/infer_service_template.yaml`（CRD 场景）中 mindie-motor-coordinator-infer 的 nodePort 端口保持一致）。

    使用物理机IP和端口号方式样例：

    ```json
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
          "content": "<think>\n好的，用户问我是谁。我",
       ...
       }
    ...
    ```

# Coordinator主备倒换

## 特性介绍

本特性通过ETCD分布式锁机制实现Kubernetes集群中Coordinator的主备倒换功能，确保系统高可用性。开启Coordinator主备倒换特性开关时，初始化时拉起两个Coordinator，通过ETCD分布式锁竞争来实现主备身份确认，当主Coordinator发生故障时，备用Coordinator能在一定时间间隔后自动接管工作。

**限制与约束**

- 主、备Coordinator节点不建议部署在同一台节点上。
- ETCD服务端需要使用v3.6版本。
- 特性生效依赖ETCD服务端正确部署，服务端至少需要3个副本，以保证ETCD集群的可靠性。
- Coordinator、Controller主备倒换特性可以共用一套ETCD；多套大EP集群可以共用一套ETCD，通过命名空间区分。

## 部署流程

### 生成ETCD安全证书（可选）

Coordinator主备倒换依赖ETCD分布式锁功能，涉及集群内不同POD间通信，建议使用CA证书做双向认证，证书配置请参考[证书生成](#生成etcd安全证书可选)。
<b>如果不开启CA证书，请跳过此步骤。</b>

### 部署ETCD服务端

ETCD服务端部署请参考[ETCD部署](#部署etcd服务端)。
>[!NOTE]说明
>Coordinator、Controller主备倒换特性可以共用一套ETCD；多套大EP集群可以共用一套ETCD，通过命名空间区分。

### 配置K8s管理端（可选）

当硬件出现故障时（如机器重启），K8s集群无法迅速感知容器Pod的状态，导致推理业务无法在指定时间内恢复，可通过执行如下步骤以加快业务恢复速度。

>[!NOTE]说明
>如果不要求硬件故障影响时长，可不执行下述步骤。

1. 执行以下命令查询K8s管理节点心跳超时标记阈值（node-monitor-grace-period），如果结果为空表示为默认值。

    ```bash
    kubectl describe pod <kube-controller-manager-pod 名> -n kube-system | grep "node-monitor-grace-period"
    ```

2. 执行以下命令打开并修改配置节点心跳超时标记阈值（node-monitor-grace-period），配置文件所在路径一般存放在控制平面节点（运行kube-controller-manager的节点）的/etc/kubernetes/manifests/kube-controller-manager.yaml目录。

    ```bash
    vi /etc/kubernetes/manifests/kube-controller-manager.yaml
    ```

      修改内容如下所示：

      ```yaml
        apiVersion: v1
        kind: Pod
        metadata:
        name: kube-controller-manager-<控制平面节点名>  # 如 kube-controller-manager-node-97-10
        namespace: kube-system
        spec:
        containers:
        - command:
            - kube-controller-manager
            # 其他原有参数...（保留不变）
            - --kubeconfig=/etc/kubernetes/controller-manager.conf
            - --authentication-kubeconfig=/etc/kubernetes/controller-manager.conf
            # 添加/修改 node-monitor-grace-period 参数（改为 20s）
            - --node-monitor-grace-period=20s
            # 其他原有参数...
      ```

3. 按`Esc`键，输入`:wq!`，按`Enter`保存并退出编辑。
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

### 配置Motor

1. 配置Coordinator侧证书挂载。

    <b>如果不开启CA证书，请跳过此步骤。</b><br>
    如果需要开启证书CA认证，根据[3.1](#生成etcd安全证书可选)生成的证书文件，将证书文件的生成路径挂载至Coordinator容器内。在 `examples/deployer/yaml_template/coordinator_template.yaml` 文件中的 volumeMounts 和 volumes 中添加如下内容（coordinator-ca 为挂载的证书目录）：

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
      - name: coordinator-ca
        hostPath:
          path: /home/{用户名}/auto_gen_ms_cert # 物理机创建文件及生成文件路径
          type: Directory
    ...
    ```

2. 配置user_config.json配置文件，开启TLS认证。<br>
    <b>如果不开启CA证书，请跳过此步骤。</b><br>
    开启CA证书认证：
    - 设置tls_config/etcd_tls_config的"enable_tls"为true；
    - 设置ca_file/cert_file/key_file/passwd_file/tls_crl为对应的文件路径。

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

3. 在user_config.json配置文件中开启Coordinator主备倒换特性，配置参数如下所示。"enable_master_standby"修改为true。

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
    >[!NOTE]说明
    > 默认使用default工作空间下的ETCD服务端，端口号默认为2379。如果需要修改，在motor_coordinator_config中修改ETCD信息。域名通常为`etcd.{namespace}.svc.cluster.local`。
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

### 启动vLLM

1. 在 `examples/deployer` 目录下执行以下命令启动。支持指定配置目录或单独指定配置文件：

    ```bash
    cd examples/deployer
    # 方式一：指定配置目录（推荐）
    python deploy.py --config_dir ../infer_engines/vllm

    # 方式二：单独指定配置文件
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    >[!NOTE]说明
    > * 可通过查询对应节点日志判断Coordinator主备节点，如果日志中出现"Role changed from standby to master"，表明当前节点抢到ETCD分布式锁，为主节点。
    > * 可通过K8s命令"kubectl get pod -A -owide"查看pod列表，有且仅有一个Coordinator pod READY状态为1/1，表示该节点为Coordinator主节点。

2. 发送请求验证服务是否启动成功。

    有以下两种方式发送请求：
    - Coordinator主节点的虚拟IP和端口号：`http://PodIP:1025`。（其中仅有READY为1/1的才可执行推理请求）
    - K8s集群内任意物理机IP:31015（端口号需与 `examples/deployer/yaml_template/coordinator_template.yaml`（multi_deployment 场景）或 `examples/deployer/yaml_template/infer_service_template.yaml`（CRD 场景）中 mindie-motor-coordinator-infer 的 nodePort 端口保持一致）。

    该样例使用物理机IP和端口号方式：

    ```json
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
          "content": "<think>\n好的，用户问我是谁。我",
       ...
       }
    ...
    ```

# ETCD集群部署

ETCD集群部署包含两部分：生成ETCD安全证书和部署ETCD服务端。

## 生成ETCD安全证书（可选）

>[!NOTE]说明
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

    >[!NOTE]说明
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

    >[!NOTE]说明
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

    >[!NOTE]说明
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

## 部署ETCD服务端

部署参考样例如下:

1. 执行以下命令加载ETCD镜像。

    ```bash
    docker pull quay.io/coreos/etcd:v3.6.0-rc.4
    ```

    >[!NOTE]说明
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
        - spec.local.path：对应节点的路径，必须真实且存在。
        - spec.nodeAffinity.required.nodeSelectorTerms.matchExpressions.values：待部署的节点名称。

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

        根据[3.1](standby.md#生成etcd安全证书可选)生成的证书，将文件生成路径挂载至ETCD容器内，并配置ETCD使用加密通信，指定使用ca.pem、server.pem和server.key进行通信。

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
                    - --listen-client-urls=https://0.0.0.0:2379 # 监管客户端请求
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
                - name: crt
                  hostPath:
                    path: /usr/local/Ascend/driver
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

        - spec.template.spec.containers.args.--client-cert-auth： 启用客户端证书认证
        - spec.template.spec.containers.args.--cert-file：指定服务端证书
        - spec.template.spec.containers.args.--key-file：指定服务端私钥
        - spec.template.spec.containers.args.--trusted-ca-file：指定信任的CA根证书
        - spec.template.spec.containers.args.--peer-client-cert-auth：启用peer节点间的客户端证书认证
        - spec.template.spec.containers.args.--peer-trusted-ca-file：指定信任的CA根证书（用于peer）
        - spec.template.spec.containers.args.--peer-cert-file：指定本节点作为peer的证书
        - spec.template.spec.containers.args.--peer-key-file：指定本节点作为peer的私钥

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

        >[!NOTE]说明
        >如果要修改ETCD集群中的yaml文件，重新创建ETCD资源，则需要先执行删除，命令如下：<br>
        > ```kubectl delete -f etcd.yaml && kubectl delete pvc --all && kubectl delete pv etcd-data-0 etcd-data-1 etcd-data-2```<br>
        > 再删除etcd-0，etcd-1，etcd-2数据库中内容:
        >
        > ```bash
        > rm -rf /mnt/data/etcd-0/*
        > rm -rf /mnt/data/etcd-1/*
        > rm -rf /mnt/data/etcd-2/*
        > ```
