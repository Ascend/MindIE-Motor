# 纯 ModelArts 2.0（只读 RankTable 版）部署 MindIE Motor PD 分离服务指南

本文档完全在 **ModelArts 2.0「多角色分离」** 页面内完成部署，直接通过平台注入的全局 RankTable（`GLOBAL_RANK_TABLE_FILE_PATH`）自动获取管理面 IP。

> [!TIP]
>
> **方案特点**：
>
> 1. **零额外组件**：不依赖 CCE、SFS 或自建 DNS。
> 2. **直接复用官方 `docker_deploy.py`**：避免手写复杂的 `prepare.sh` 与大量 `sed` 脚本。

---

## 1. 原理说明：如何从 RankTable 拿到管理面 IP

ModelArts 2.0 多角色分离服务启动时，平台会往容器内注入环境变量 `GLOBAL_RANK_TABLE_FILE_PATH`（默认路径通常为 `/user/global/config/global_rank_table.json`，详细字段说明参见[华为云 ModelArts 2.0 RankTable 文档](https://support.huaweicloud.com/inference-modelarts/inference2.0-modelarts-0080.html)），结构如下：

```json
{
  "status": "completed",
  "server_group_list": [
    {
      "group_id": "0",
      "server_count": "1",
      "server_list": [
        {
          "server_id": "192.168.x.x",
          "server_ip": "172.16.x.x",
          "pod_name": "infer-xxx-role-0-xxx",
          "device": [...]
        }
      ]
    }
  ]
}
```

- **`status`**：为 `"completed"` 时表示所有节点拓扑已就绪。
- **`server_ip`**：Pod IP（**注意不要用 `server_id`，`server_id` 为宿主机 IP**）。
- **`pod_name` / `group_id`**：可用于定位 `role-0`（管理面 `coordinator_controller`）对应的节点 IP。

> [!IMPORTANT]
> **使用前提确认**：部分版本的 ModelArts 仅将带 NPU 的单元写入 `global_rank_table.json`。首次部署时请确认 `role-0` 的 `pod_name`（或 `group_id: "0"`）已出现在该文件中；若纯 CPU 规格未写入 RankTable，可为 `role-0` 选用最小 NPU 规格。

---

## 2. 推理单元规划

在 ModelArts 2.0「单元配置」中选择 **「多角色分离」**，配置以下 **3 个单元**：

| 推理单元 | Motor 角色 | 规格类型 | 单元副本数 | 关键说明 |
| :--- | :--- | :--- | :--- | :--- |
| `role-0`（入口单元） | `coordinator_controller` | CPU 规格（若未进 RankTable 则选最小 NPU 规格） | `1` | 对外暴露 `1025` 端口，负责请求调度与全集群注册管理 |
| `role-1` | `prefill` | 昇腾 NPU 规格 | 按 `p_instances_num` | 从 RankTable 读取 `role-0` 的 `server_ip` 并注册 |
| `role-2` | `decode` | 昇腾 NPU 规格 | 按 `d_instances_num` | 从 RankTable 读取 `role-0` 的 `server_ip` 并注册 |

---

如下图所示：

![ModelArts 2.0 推理单元配置示例](https://raw.gitcode.com/user-images/assets/11404067/c1409290-0476-4175-a1a6-49ce6d09819e/image.png 'image.png')

其中单元副本数对应 `p_instances_num` / `d_instances_num`，单副本资源实例数对应 `single_p_instance_pod_num` / `single_d_instance_pod_num`。

## 3. OBS 静态目录准备（只读即可）

在 OBS 中准备模型权重和 Motor 配置文件，挂载到各容器 `/mnt/obs/motor`：

```text
/mnt/obs/motor/
├── models/<模型目录>/          # 模型权重
└── config/
    ├── user_config.json        # Motor 集群与引擎配置
    └── env.json                # 环境变量配置
```

> [!NOTE]
>
> - **错开管理面端口（必选）**：由于 `role-0` 在同一 Pod 内分别独立启动 `coordinator` 与 `controller` 进程，为避免与 Coordinator 默认端口（`1025`/`1026`/`1027`）冲突，并让探针脚本读到准确端口，请在 `user_config.json` 中显式配置 Controller 管理端口：
>
>   ```json
>   "motor_controller_config": {
>     "api_config": { "controller_api_port": 2026, "observability_api_port": 2027 }
>   }
>   ```
>
> - 若使用 **HTTP** 对外提供服务，请确认 `user_config.json` 中关闭推理面 TLS（`motor_deploy_config.tls_config.infer_tls_config.enable_tls: false`）；若使用 **HTTPS**，则保持开启并配置证书。
> - 启动命令会先把 `/mnt/obs/motor/config/*` 拷贝到容器本地临时目录（`role-0` 为 `/tmp/motor_config_coord` 与 `/tmp/motor_config_ctrl`，`role-1`/`role-2` 为 `/tmp/motor_config`），因此 OBS 保持**只读权限**即可。

---

## 4. ModelArts 2.0 页面配置步骤

> [!NOTE]
> 若在「更多配置」中指定了非 root 用户运行容器，请确认容器内 `/tmp/motor/examples` 目录可写（`docker_deploy.py --start` 会在该目录下创建 `motor_workspace`；默认 `root` 运行可忽略）。

### 4.1 配置 `role-0`（管理面：`coordinator` + `controller` 独立守护）

1. **单元副本数**：`1`；**单副本资源实例数**：`1`。
2. **单元实例规格**：选择 CPU 规格（或满足写入 RankTable 要求的规格）。
3. **镜像类型**：选择 `mindie-motor` 镜像。
4. **文件存储挂载**：挂载 OBS 到 `/mnt/obs/motor`（**只读**）。
5. **健康检查**（推荐开启）：选择 **「执行命令检查」** 探测管理端口（`1026` 与 `2026`），详见第 `4.4` 节。
6. **启动命令**（从 RankTable 获取自身 `POD_IP`，并为 `coordinator` 与 `controller` 分别启用**独立原地重启循环**，避免原有 `coordinator_controller` 模式下单进程退出连带杀掉兄弟进程导致容器重建换 IP）：

```bash
set -euo pipefail
mkdir -p /tmp/motor_config_coord /tmp/motor_config_ctrl
cp -r /mnt/obs/motor/config/* /tmp/motor_config_coord/
cp -r /mnt/obs/motor/config/* /tmp/motor_config_ctrl/

RANK_TABLE="${GLOBAL_RANK_TABLE_FILE_PATH:-/user/global/config/global_rank_table.json}"
echo "Waiting for MA ranktable: $RANK_TABLE"

POD_IP=""
for i in $(seq 1 300); do
  if [ -s "$RANK_TABLE" ]; then
    POD_IP=$(python3 - "$RANK_TABLE" <<'PY'
import json, os, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
except (json.JSONDecodeError, OSError):
    sys.exit(0)
if data.get("status") != "completed":
    sys.exit(0)
hostname = os.environ.get("HOSTNAME", "")
groups = data.get("server_group_list") or []
for g in groups:
    for s in g.get("server_list") or []:
        if hostname and s.get("pod_name") == hostname:
            print(s.get("server_ip", ""))
            sys.exit(0)
for g in groups:
    for s in g.get("server_list") or []:
        if "role-0" in (s.get("pod_name") or ""):
            print(s.get("server_ip", ""))
            sys.exit(0)
PY
) || POD_IP=""
    if [ -n "$POD_IP" ]; then
      break
    fi
  fi
  sleep 2
done

if [ -z "$POD_IP" ]; then
  echo "Timeout waiting for role-0 POD_IP from $RANK_TABLE (HOSTNAME=${HOSTNAME:-})" >&2
  exit 1
fi

# 用 Python 标准库反查 POD_IP 对应的内核网卡名（不依赖 iproute2，天然不含 @if 后缀）
NIC=$(POD_IP="$POD_IP" python3 - <<'PY'
import fcntl, os, socket, struct
target = os.environ["POD_IP"]
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    for _, name in socket.if_nameindex():
        try:
            info = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode("utf-8")[:15]))
            if socket.inet_ntoa(info[20:24]) == target:
                print(name)
                break
        except OSError:
            continue
PY
) || NIC=""
if [ -z "$NIC" ]; then
  echo "Failed to resolve network interface for POD_IP=$POD_IP" >&2
  exit 1
fi

echo "$POD_IP" > /tmp/motor_pod_ip

trap 'kill 0' INT TERM

# 各自使用独立配置副本、独立 --container-name（避免 workspace 竞争）与守护循环
# 注：双循环模式要求容器运行时为 containerd（不存在 /.dockerenv）；若底层为 Docker 运行时，建议将两者拆分为两个独立单元
# 注：两者共享 /usr/local/Ascend/Motor/conf，若首次并发拉起偶发配置读取冲突，2s 后自动重启一次即可恢复
(
  while true; do
    python3 /tmp/motor/examples/deployer/docker_deploy.py \
      --config_dir /tmp/motor_config_coord \
      --container-name motor-coord \
      --role coordinator \
      --pod-ip "$POD_IP" \
      --nic-name "$NIC" \
      --controller-ip "$POD_IP" \
      --coordinator-ip "$POD_IP" \
      --start || true
    echo "[WARN] coordinator exited, restarting in 2s..." >&2
    sleep 2
  done
) &

(
  while true; do
    python3 /tmp/motor/examples/deployer/docker_deploy.py \
      --config_dir /tmp/motor_config_ctrl \
      --container-name motor-ctrl \
      --role controller \
      --pod-ip "$POD_IP" \
      --nic-name "$NIC" \
      --controller-ip "$POD_IP" \
      --coordinator-ip "$POD_IP" \
      --start || true
    echo "[WARN] controller exited, restarting in 2s..." >&2
    sleep 2
  done
) &

wait
```

---

启动命令可直接粘贴至页面对应输入框：

![ModelArts 2.0 role-0 启动命令示例](https://raw.gitcode.com/user-images/assets/11404067/c058b5e2-3a33-4f3d-96a8-218802c988c2/image.png 'image.png')

### 4.2 配置 `role-1`（Prefill 单元）

点击 **「+ 添加推理单元」**，配置如下：

1. **单元副本数**：按 `user_config.json` 中 `p_instances_num` 填写（例如 `1`）。
2. **单副本资源实例数**：单机实例填 `1`；若单个 P 实例跨多机，按单实例节点数填写。
3. **单元实例规格**：选择昇腾 NPU 规格（建议勾选 **「绑核加速」**）。
4. **镜像类型**：同 `role-0`，使用 `mindie-motor` 镜像。
5. **文件存储挂载**：挂载 OBS 到 `/mnt/obs/motor`（**只读**）。
6. **启动命令**（从 RankTable 一次性解析 `CTRL_IP` 与自身 `POD_IP`）：

```bash
set -euo pipefail
mkdir -p /tmp/motor_config
cp -r /mnt/obs/motor/config/* /tmp/motor_config/

RANK_TABLE="${GLOBAL_RANK_TABLE_FILE_PATH:-/user/global/config/global_rank_table.json}"
UNIT_NAME="role-1"  # 若在页面修改了推理单元名称，请同步修改此处
echo "Waiting for MA ranktable: $RANK_TABLE"

CTRL_IP=""
POD_IP=""
INSTANCE_NAME=""
for i in $(seq 1 300); do
  if [ -s "$RANK_TABLE" ]; then
    read -r CTRL_IP POD_IP INSTANCE_NAME < <(python3 - "$RANK_TABLE" "$UNIT_NAME" <<'PY'
import json, os, re, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
except (json.JSONDecodeError, OSError):
    sys.exit(0)
if data.get("status") != "completed":
    sys.exit(0)
hostname = os.environ.get("HOSTNAME", "")
unit_name = sys.argv[2]
groups = data.get("server_group_list") or []
ctrl_ip, self_ip = "", ""
for g in groups:
    for s in g.get("server_list") or []:
        ip = s.get("server_ip", "")
        pname = s.get("pod_name", "")
        if not ctrl_ip and "role-0" in pname:
            ctrl_ip = ip
        if not self_ip and hostname and pname == hostname:
            self_ip = ip
if ctrl_ip and self_ip:
    # 提取 KubeInfer 多机大 EP 副本标识（Leader: <unit>-<replica>，Worker: <unit>-<replica>-<worker>）
    # 未匹配到时默认失败退出，避免大 EP 副本被静默拆散；单机随机后缀场景可设 INS_FROM_HOSTNAME=1
    m = re.search(rf"({re.escape(unit_name)}-\d+)(?:-\d+)?$", hostname)
    if m:
        print(f"{ctrl_ip} {self_ip} prefill-{m.group(1)}")
    elif os.environ.get("INS_FROM_HOSTNAME") == "1":
        print(f"{ctrl_ip} {self_ip} prefill-{hostname}")
PY
) || true
    if [ -n "$CTRL_IP" ] && [ -n "$POD_IP" ] && [ -n "$INSTANCE_NAME" ]; then
      break
    fi
  fi
  sleep 2
done

if [ -z "$CTRL_IP" ] || [ -z "$POD_IP" ] || [ -z "$INSTANCE_NAME" ]; then
  echo "Timeout waiting for CTRL_IP/POD_IP/INSTANCE_NAME from $RANK_TABLE (HOSTNAME=${HOSTNAME:-}, UNIT_NAME=$UNIT_NAME)" >&2
  head -c 512 "$RANK_TABLE" >&2 || true
  exit 1
fi

NIC=$(POD_IP="$POD_IP" python3 - <<'PY'
import fcntl, os, socket, struct
target = os.environ["POD_IP"]
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    for _, name in socket.if_nameindex():
        try:
            info = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode("utf-8")[:15]))
            if socket.inet_ntoa(info[20:24]) == target:
                print(name)
                break
        except OSError:
            continue
PY
) || NIC=""
if [ -z "$NIC" ]; then
  echo "Failed to resolve network interface for POD_IP=$POD_IP" >&2
  exit 1
fi

echo "$POD_IP" > /tmp/motor_pod_ip

python3 /tmp/motor/examples/deployer/docker_deploy.py \
  --config_dir /tmp/motor_config \
  --role prefill \
  --instance-name "$INSTANCE_NAME" \
  --pod-ip "$POD_IP" \
  --nic-name "$NIC" \
  --controller-ip "$CTRL_IP" \
  --coordinator-ip "$CTRL_IP" \
  --start
```

---

### 4.3 配置 `role-2`（Decode 单元）

点击 `role-1` 右上角 **「克隆」** 生成 `role-2`：

1. **单元副本数** / **单副本资源实例数**：按 `d_instances_num` 和单 D 实例节点数填写。
2. **启动命令**：与 `role-1` 相同，只需将 `UNIT_NAME="role-1"` 改为 `UNIT_NAME="role-2"`，并将 `prefill` 改为 `decode`：

```bash
...
UNIT_NAME="role-2"  # 若在页面修改了推理单元名称，请同步修改此处
# 并将 Python 片段中的 prefill- 改为 decode-
...
python3 /tmp/motor/examples/deployer/docker_deploy.py \
  --config_dir /tmp/motor_config \
  --role decode \
  --instance-name "$INSTANCE_NAME" \
  --pod-ip "$POD_IP" \
  --nic-name "$NIC" \
  --controller-ip "$CTRL_IP" \
  --coordinator-ip "$CTRL_IP" \
  --start
```

> [!NOTE]
>
> **`--instance-name` 与 KubeInfer 多机大 EP 副本机制说明**（具体行为以控制台及集群插件版本为准）：
>
> - 在 KubeInfer（7.5.2+）多机大 EP 多副本下，Leader Pod 为 `<前缀>-<单元名>-<副本号>`，Worker Pod 为 `<前缀>-<单元名>-<副本号>-<节点号>`；上述正则会自动提取 `<单元名>-<副本号>`，保证同副本内的 Leader 与所有 Worker 共用同一 `--instance-name`，不同副本互不冲突（命名与重建规则参考[华为云 ModelArts 2.0 推理单元与 KubeInfer 文档](https://support.huaweicloud.com/inference-modelarts/inference2.0-modelarts-0096.html)及 [RankTable 文档](https://support.huaweicloud.com/inference-modelarts/inference2.0-modelarts-0080.html)）。
> - 若为单机 Deployment 随机后缀命名（不匹配上述格式），请在页面环境变量中添加 `INS_FROM_HOSTNAME=1`（默认关闭），以回退使用完整 `$HOSTNAME` 生成实例名。
> - 当 Worker 异常时，Motor Controller 的 `NmSuicideStrategy` 会联动同实例 Leader Pod 退出，从而触发 KubeInfer 将整个单元副本下所有 Pod 一起重建恢复。

---

### 4.4 各角色健康检查探针配置（执行命令检查）

由于各组件默认绑定 `POD_IP`（而非 `0.0.0.0`），且管理端口各不相同（Coordinator 默认 `1026`、Controller 按第 `3` 节配置为 `2026`、NodeManager 默认 `1026`），同时页面上的「HTTP 请求检查」若无独立端口配置项会探测容器入口端口（以控制台实际界面为准），因此建议三个单元统一选择 **「执行命令检查」**，直接调用镜像自带的 `probe.py`（自动读取端口、支持 TLS、不依赖 `curl`）：

探针命令统一读取启动脚本写入 `/tmp/motor_pod_ip` 的 `POD_IP`，避免多网卡下 `hostname -I` 取偏。

**`role-0`（`coordinator` + `controller`）启动探针**

```bash
bash -c 'POD_IP=$(cat /tmp/motor_pod_ip); export POD_IP; CONFIG_PATH=/tmp/motor_config_coord python3 /tmp/motor/examples/deployer/probe/probe.py coordinator startup && CONFIG_PATH=/tmp/motor_config_ctrl python3 /tmp/motor/examples/deployer/probe/probe.py controller startup'
```

- 就绪探针：将上述命令中的 `startup` 替换为 `readiness`。
- 存活探针：将上述命令中的 `startup` 替换为 `liveness`。

**`role-1`（`prefill`）启动探针**

```bash
bash -c 'POD_IP=$(cat /tmp/motor_pod_ip) CONFIG_PATH=/tmp/motor_config python3 /tmp/motor/examples/deployer/probe/probe.py prefill readiness'
```

- 就绪探针：同上（`readiness`）。
- 存活探针：将上述命令中的 `readiness` 替换为 `liveness`。

**`role-2`（`decode`）**：与 `role-1` 相同，只需将 `prefill` 替换为 `decode`。

**P/D 启动探针说明**：`probe.py` 默认的 `startup` 探测 `/node-manager/status`（NodeManager 拉起即返回 `200`，早于引擎权重加载完成）；上述配置直接以 `readiness` 作为启动探针，可覆盖完整模型加载缓冲期，避免引擎未就绪时提前接入 `livenessProbe` 检测。

**超时与阈值建议**（关键，勿使用默认 `3` 次；探针超时建议不小于 `30s`）：

- **启动探针**：大模型从 OBS 加载权重与 NPU 建链耗时较长，建议设置 **检查周期 `10s`、延时 `30s`、超时 `30s`、最大失败次数 `100`~`180`**（预留 16~30 分钟启动缓冲）。
- **就绪探针**：检查周期 `10s`、延时 `30s`、超时 `30s`、最大失败次数 `5`。
- **存活探针**：检查周期 `10s`、延时 `30s`、超时 `30s`、最大失败次数 `5`（避免高负载抖动时误重启容器）。

---

### 4.5 自动重启配置

![ModelArts 2.0 自动重启配置示例](https://raw.gitcode.com/user-images/assets/11404067/8f6dae23-f815-4f88-9af5-9760662075e2/image.png 'image.png')

在每个单元配置中填写：

- `role-0`：部署副本重建
- `role-1`：单元副本重建
- `role-2`：单元副本重建

---

### 4.6 部署管理配置

在页面底部的 **「部署管理配置」** 中填写：

- **容器协议**：`HTTP` 或 `HTTPS`（须与 `user_config.json` 中 `motor_deploy_config.tls_config.infer_tls_config` 一致）。
- **容器端口**：填写 **`1025`**（Coordinator 对外推理端口）。
- **部署超时时间（分钟）**：默认 `60` 分钟一般够用，超大模型权重加载可按需调大。

点击右下角 **「确认部署」**。

---

## 5. 部署验证与注意事项

1. **推理测试**（服务状态变为「运行中」后）：

   ```bash
   curl -X POST "<ModelArts服务访问地址>/v1/chat/completions" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "<user_config.json中配置的模型名>",
       "messages": [{"role": "user", "content": "你好，请介绍一下你自己"}],
       "max_tokens": 64
     }'
   ```

2. **多机通信（HCCS vs RoCE）**：同实例跨多机且无跨机 HCCS 时，在 `role-1` / `role-2` 的「环境变量」中添加 `HCCL_INTER_HCCS_DISABLE=true` 走 RoCE；A3 超节点保持默认即可。
3. **排障建议**：若出现 `Timeout waiting for role-0 IP`，先进入 `role-1` 容器查看 `echo $GLOBAL_RANK_TABLE_FILE_PATH` 及文件内容，确认 `role-0` 是否已写入 `server_group_list`（重点核对纯 CPU 单元是否被平台写入 RankTable）。
