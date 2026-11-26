# 主备倒换特性

以下为 K8s 主备部署指导；Docker-only 请参见 [Docker 主备部署指南](../../deployment/docker/ha.md)。

## 特性介绍

主备倒换通过备用实例自动接管故障组件的工作，减少人工恢复操作，缩短Controller或Coordinator故障造成的服务中断时间。主备Pod跨节点部署时，也可在单个节点故障后由另一节点上的备用实例接管。倒换期间仍可能出现短暂请求失败，业务侧需具备重试能力。

Controller和Coordinator通过ETCD分布式锁选举主节点。开启主备后，部署工具为对应组件创建2个Pod：主节点提供服务，备节点等待接管；主节点故障后，备节点在租约失效并取得锁后接管工作。

两个组件可以共用一套ETCD，选主过程互不影响；可以单独开启主备，也可以同时开启。

### 约束与限制

- 依赖可用的ETCD服务，建议部署3个副本。按本文示例部署ETCD，需要3个可调度节点，每个节点准备一个本地数据目录，以容忍单个节点故障。
- Motor主备自身需要至少2个满足调度条件的节点，Controller、Coordinator各自的主备Pod须跨节点部署，可与ETCD共用节点。若集群只有2个节点，请使用已有的外部ETCD服务，不能直接套用本文的ETCD三副本部署示例。

## 部署指导

### 配置ETCD证书（可选）

按使用环境选择：

- **隔离测试环境：可不使用证书**，跳过本节，直接部署ETCD。
- **正式生产环境：使用证书**，先完成本节，再部署ETCD。

> 本文中不使用证书的示例采用明文传输且不校验客户端身份，网络可达的客户端均可读写主备锁和持久化状态，仅用于隔离的测试环境。生产环境应启用下文所述的双向证书认证，并通过网络策略（如NetworkPolicy）限制2379端口仅供Motor及必要的运维客户端访问，2380端口仅供ETCD成员间通信。

#### 1. 生成证书

在安装了OpenSSL的机器上执行以下命令。示例生成一个CA、一份ETCD服务端/成员证书和一份Motor客户端证书，3个ETCD成员共用服务端证书。

```bash
mkdir -p motor-etcd-certs
cd motor-etcd-certs
umask 077

openssl req -x509 -newkey rsa:3072 -nodes \
  -keyout ca.key -out ca.pem -days 3650 -subj '/CN=motor-etcd-ca' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign'

cat > server.ext <<'CERT'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=IP:127.0.0.1,DNS:etcd.default.svc.cluster.local,DNS:etcd-0.etcd.default.svc.cluster.local,DNS:etcd-1.etcd.default.svc.cluster.local,DNS:etcd-2.etcd.default.svc.cluster.local
CERT
openssl req -newkey rsa:3072 -nodes -keyout server.key \
  -out server.csr -subj '/CN=motor-etcd-server'
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.pem -days 365 -extfile server.ext

cat > client.ext <<'CERT'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
CERT
openssl req -newkey rsa:3072 -nodes -keyout client.key \
  -out client.csr -subj '/CN=motor-etcd-client'
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAserial ca.srl \
  -out client.pem -days 365 -extfile client.ext

openssl verify -CAfile ca.pem server.pem client.pem
```

最后应输出两条`OK`。证书中的`127.0.0.1`供ETCD容器内的就绪探针使用。如果修改了ETCD的Service名称、命名空间或集群域名，需要相应修改`server.ext`中的DNS名称。妥善保存`ca.key`，不要将CA私钥分发到业务节点或容器中。

#### 2. 保存ETCD证书

在证书目录执行以下命令，将ETCD所需文件保存到Kubernetes中，供部署时挂载：

```bash
kubectl -n default create secret generic etcd-server-tls \
  --from-file=ca.pem --from-file=server.pem --from-file=server.key
```

### 部署ETCD三副本

建议使用ETCD v3.6。以下示例在`default`命名空间部署3个ETCD副本，每个副本使用所在节点的本地目录保存数据。

#### 1. 准备三个节点的数据目录

执行`kubectl get nodes`查看节点名称，选择3个可调度节点。在这3台机器上分别执行：

```bash
sudo mkdir -p /mnt/motor-etcd-data
```

该目录专用于本次新建的ETCD集群，首次部署时应为空。下面的配置会将每个副本固定到对应节点，Pod重建后仍使用原目录中的数据。

在能执行`kubectl`的机器上，将以下`node-a`、`node-b`、`node-c`替换为选定的节点名称，然后执行整段命令。该命令会把3个本地目录登记为Kubernetes可用的数据卷，无需另外安装存储组件。

```bash
nodes=(node-a node-b node-c) # 必须替换为实际选择的3个节点名称
for i in 0 1 2; do
  cat <<YAML
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: motor-etcd-data-${i}
spec:
  capacity:
    storage: 4Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef:
    namespace: default
    name: data-etcd-${i}
  local:
    path: /mnt/motor-etcd-data
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: ["${nodes[$i]}"]
YAML
done > etcd-volumes.yaml
kubectl apply -f etcd-volumes.yaml
```

上述节点名称应与节点的`kubernetes.io/hostname`标签一致（通常一致），可通过`kubectl get nodes -L kubernetes.io/hostname`查看。

#### 2. 保存部署配置

将以下内容保存为`etcd.yaml`：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: etcd
  namespace: default
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    app: etcd
  ports:
    - name: client
      port: 2379
    - name: peer
      port: 2380
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: etcd
  namespace: default
spec:
  serviceName: etcd
  replicas: 3
  podManagementPolicy: Parallel
  selector:
    matchLabels:
      app: etcd
  template:
    metadata:
      labels:
        app: etcd
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: etcd
              topologyKey: kubernetes.io/hostname
      containers:
        - name: etcd
          image: quay.io/coreos/etcd:v3.6.0
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: SCHEME
              value: http
          command: [/usr/local/bin/etcd]
          args:
            - --name=$(POD_NAME)
            - --data-dir=/data
            - --listen-client-urls=$(SCHEME)://0.0.0.0:2379
            - --advertise-client-urls=$(SCHEME)://$(POD_NAME).etcd.default.svc.cluster.local:2379
            - --listen-peer-urls=$(SCHEME)://0.0.0.0:2380
            - --initial-advertise-peer-urls=$(SCHEME)://$(POD_NAME).etcd.default.svc.cluster.local:2380
            - --initial-cluster=etcd-0=$(SCHEME)://etcd-0.etcd.default.svc.cluster.local:2380,etcd-1=$(SCHEME)://etcd-1.etcd.default.svc.cluster.local:2380,etcd-2=$(SCHEME)://etcd-2.etcd.default.svc.cluster.local:2380
            - --initial-cluster-state=new
            - --initial-cluster-token=motor-etcd
          ports:
            - name: client
              containerPort: 2379
            - name: peer
              containerPort: 2380
          readinessProbe:
            exec:
              command:
                - /usr/local/bin/etcdctl
                - --endpoints=http://127.0.0.1:2379
                - endpoint
                - health
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          resources:
            requests:
              cpu: "500m"
              memory: "512Mi"
            limits:
              cpu: "2"
              memory: "2Gi"
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: ""
        resources:
          requests:
            storage: 4Gi
```

上述资源值用于示例，可根据ETCD实际负载调整。就绪探针检查本地成员能否正常处理请求。

#### 3. 配置证书挂载（可选）

如果前面已准备证书，在刚保存的`etcd.yaml`中修改以下四处；不使用证书则跳过此步：

- 将`SCHEME`的`value`从`http`改为`https`。
- 在ETCD容器原有`args`列表末尾追加：

  ```yaml
  - --client-cert-auth=true
  - --trusted-ca-file=/etc/etcd/tls/ca.pem
  - --cert-file=/etc/etcd/tls/server.pem
  - --key-file=/etc/etcd/tls/server.key
  - --peer-client-cert-auth=true
  - --peer-trusted-ca-file=/etc/etcd/tls/ca.pem
  - --peer-cert-file=/etc/etcd/tls/server.pem
  - --peer-key-file=/etc/etcd/tls/server.key
  ```

- 在ETCD容器原有`volumeMounts`列表中追加证书挂载，并在`spec.template.spec`下添加`volumes`（与`containers`同级）：

  ```yaml
  # 追加到etcd容器的volumeMounts列表
  - name: etcd-tls
    mountPath: /etc/etcd/tls
    readOnly: true
  ```

  ```yaml
  # 添加到spec.template.spec
  volumes:
    - name: etcd-tls
      secret:
        secretName: etcd-server-tls
  ```

- 将ETCD容器的`readinessProbe`替换为以下配置，使用HTTPS和证书检查本地成员：

  ```yaml
  readinessProbe:
    exec:
      command:
        - /usr/local/bin/etcdctl
        - --endpoints=https://127.0.0.1:2379
        - --cacert=/etc/etcd/tls/ca.pem
        - --cert=/etc/etcd/tls/server.pem
        - --key=/etc/etcd/tls/server.key
        - endpoint
        - health
    initialDelaySeconds: 5
    periodSeconds: 10
    timeoutSeconds: 5
    failureThreshold: 3
  ```

#### 4. 部署并检查健康状态

```bash
kubectl apply -f etcd.yaml
kubectl -n default rollout status statefulset/etcd --timeout=300s
kubectl -n default get pods -l app=etcd -o wide
```

未启用证书时，使用以下命令检查：

```bash
kubectl -n default exec etcd-0 -- etcdctl \
  --endpoints=http://etcd-0.etcd.default.svc.cluster.local:2379,http://etcd-1.etcd.default.svc.cluster.local:2379,http://etcd-2.etcd.default.svc.cluster.local:2379 \
  endpoint health
```

启用证书时，使用以下命令检查：

```bash
kubectl -n default exec etcd-0 -- etcdctl \
  --endpoints=https://etcd-0.etcd.default.svc.cluster.local:2379,https://etcd-1.etcd.default.svc.cluster.local:2379,https://etcd-2.etcd.default.svc.cluster.local:2379 \
  --cacert=/etc/etcd/tls/ca.pem \
  --cert=/etc/etcd/tls/server.pem --key=/etc/etcd/tls/server.key \
  endpoint health
```

确认3个Pod运行在不同节点，且3个endpoint均返回`is healthy`。

## 启用主备倒换特性

将以下配置片段合并到已有的`user_config.json`中，并保留模型、引擎等其他配置。

### 1. 开启Controller、Coordinator主备

```json
{
  "motor_controller_config": {
    "standby_config": {
      "enable_master_standby": true
    }
  },
  "motor_coordinator_config": {
    "standby_config": {
      "enable_master_standby": true
    }
  }
}
```

如果只需要为其中一个组件开启主备，则仅将该组件的开关设为`true`。开启后，部署工具会自动将对应组件副本数设为2，组件会自动开启ETCD状态持久化。

**配置主备Pod跨节点部署。** `multi_deployment`中Controller、Coordinator的默认模板已配置强制反亲和性，无需额外修改。`infer_service_set`的默认模板未配置，需要在启动部署前修改`examples/deployer/yaml_template/infer_service_template.yaml`：

- 在`spec.template.roles`中找到`name: controller`，将以下配置合并到该角色的`spec.template.spec`下，与`containers`同级：

  ```yaml
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app: mindie-motor-controller
          topologyKey: kubernetes.io/hostname
  ```

- 找到`name: coordinator`，在该角色的`spec.template.spec`下合并以下配置：

  ```yaml
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app: mindie-motor-coordinator
          topologyKey: kubernetes.io/hostname
  ```

仅需为开启主备的组件配置。若使用自定义模板，保留已有调度约束，并确保上述`matchLabels`与对应组件的Pod标签一致。准备2个节点并不保证Pod自动分散；两个节点都必须满足该组件的节点选择器和资源要求，且组件Pod必须容忍节点上阻止调度的污点，否则备Pod会处于Pending状态。

**仅在隔离的测试环境中，按本文部署ETCD且未启用证书认证时**，开启主备开关即可，无需添加ETCD连接参数和证书配置。两个组件默认连接`etcd.default.svc.cluster.local:2379`，默认不启用ETCD TLS。该方式不校验客户端身份，生产环境请按下一节配置证书接入。

如果复用已有ETCD，或将ETCD部署在其他命名空间，则需要填写实际地址。例如，将ETCD部署在`other-namespace`命名空间时，在两个组件配置中分别添加以下内容；按本文部署则跳过：

```json
{
  "motor_controller_config": {
    "etcd_config": {
      "etcd_host": "etcd.other-namespace.svc.cluster.local",
      "etcd_port": 2379
    }
  },
  "motor_coordinator_config": {
    "etcd_config": {
      "etcd_host": "etcd.other-namespace.svc.cluster.local",
      "etcd_port": 2379
    }
  }
}
```

`etcd_host`只填写主机名或IP，不带`http://`或`https://`；启用TLS时，该地址必须包含在服务端证书的SAN中。

### 2. 连接启用证书的ETCD（可选）

如果执行了前面的ETCD证书配置，还需让Controller和Coordinator读取客户端证书。

**准备证书文件，无需重复修改Motor挂载模板。** 默认`infer_service_set`和`multi_deployment`模板均已将宿主机的`/mnt`挂载到Controller、Coordinator容器的`/mnt`。在所有可能调度这两个组件的节点上，将前面生成的`ca.pem`、`client.pem`、`client.key`放到`/mnt/motor-etcd-tls/`，保证文件内容一致、容器运行用户可读，且私钥仅对所需用户开放。

`/mnt`是各节点的本地目录，不会自动同步；若只有当前节点放置了证书，主备切换到另一节点后仍会连接失败。只分发上述3个文件，不要复制整个证书生成目录。

**在`user_config.json`中添加以下配置，两个组件共用一份ETCD TLS设置：**

```json
{
  "motor_deploy_config": {
    "tls_config": {
      "etcd_tls_config": {
        "enable_tls": true,
        "ca_file": "/mnt/motor-etcd-tls/ca.pem",
        "cert_file": "/mnt/motor-etcd-tls/client.pem",
        "key_file": "/mnt/motor-etcd-tls/client.key"
      }
    }
  }
}
```

注意`tls_config`位于`motor_deploy_config`之下，不在JSON顶层。ETCD客户端读取上述4个字段；客户端私钥使用前面生成的未加密PEM文件，无需配置`passwd_file`或`crl_file`。

如果使用自定义模板且没有`/mnt`挂载，则需要给Controller、Coordinator分别增加证书挂载，或使用Secret卷，并将上述路径改为容器内的实际路径。

### 3. 启动并验证

按[部署工具说明](../../../../../examples/deployer/README.md)完成Motor部署前置准备，再使用修改后的配置目录启动。例如在仓库根目录执行：

```bash
cd examples/deployer
python deploy.py --config_dir ../../model_configs/vllm
```

ETCD TLS 用于保护 Motor 与 ETCD 之间的通信。请按[部署前安全预检](../../deployment/k8s/README.md#deployment-security-precheck)配置推理 TLS 和管理 API Key 认证。仅在隔离测试环境需要沿用现有配置时，可在原命令末尾追加 `--skip-check`。

查看Motor所在命名空间（与`motor_deploy_config.job_id`一致）的Pod：

```bash
kubectl -n {Motor命名空间} get pods -o wide
kubectl -n {Motor命名空间} logs {Pod名} | grep 'Role changed'
```

开启主备的每个组件应各有2个Pod，分布在不同节点。正常情况下，一个为主节点，READY列为`1/1`；另一个为备节点，READY列为`0/1`。升主日志包含`Role changed from standby to master`。

可在允许中断的测试环境删除一个主Pod验证倒换：

```bash
kubectl -n {Motor命名空间} delete pod {主Pod名}
```

观察原备Pod升为主Pod、READY列变为`1/1`，并通过原有推理入口确认请求恢复。倒换需等待租约失效和服务就绪，期间请求可能短暂失败，业务侧应具备重试能力。

## 常见问题

| 现象 | 处理方法 |
|------|----------|
| 备Pod长期为`0/1` | 正常，备节点不承接业务；此时应存在一个正常就绪的主Pod。 |
| 两个Pod长期均为`0/1` | 检查ETCD的3个endpoint健康状态、连接地址、网络和组件日志；启用TLS时再检查证书路径、有效期、CA及服务端SAN。不能仅凭READY断定选主失败。 |
| 启用TLS后连接失败 | 确认服务端与客户端均启用TLS；配置位于`motor_deploy_config.tls_config.etcd_tls_config`，每个候选节点上均已放置客户端证书，且容器用户可读取。 |
| ETCD Pod一直处于Pending状态 | 确认已准备3个可调度节点及各自的数据目录，检查本地卷的节点绑定和Pod调度事件。 |
| Motor备Pod一直处于Pending状态 | 检查可调度节点数量、资源和污点，确认满足主备跨节点的反亲和约束。 |
| 修改主备开关后未生效 | 重新生成部署资源并重建对应Pod；仅修改配置文件不会改变已运行进程的主备模式或Pod副本数。 |
