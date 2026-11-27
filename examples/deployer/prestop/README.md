# PreStop 优雅下线

Engine Pod（P/D/union）在 K8s 删除、缩容或滚动更新时，通过 `lifecycle.preStop` 执行本目录脚本，实现**停止接新请求 + 等在途推理完成 + 退出容器**。

## 文件说明

| 文件 | 作用 |
|------|------|
| `prestop.sh` | K8s preStop 入口：找 NM PID、调用 `prestop.py`、发 SIGTERM 退出 NM |
| `prestop.py` | 核心逻辑：调 NM 暂停、轮询本机 engine metrics、等待排空 |

部署时由 `deploy.py` 将两个文件打入 ConfigMap（`k8s_utils.py`），Pod 内路径为 `$CONFIGMAP_PATH/prestop.sh`。

## 工作流程

```text
K8s preStop
  → prestop.sh
    → ps 查找 NM PID（grep NodeManager）
    → prestop.py POST /node-manager/pause（endpoint → PAUSED，控制面不再调度新请求）
    → 轮询本 Pod engine GET /metrics（waiting + running == 0 或超时 15s）
    → kill -TERM NM（signal_handler → 停 Daemon/Heartbeat → 退出）
  → Pod 退出
```

## 使用方式

### 自动（推荐）

`deploy.py` 部署后，Engine YAML 已配置：

```yaml
lifecycle:
  preStop:
    exec:
      command: ["bash", "-c", "$CONFIGMAP_PATH/prestop.sh"]
terminationGracePeriodSeconds: 30
```

### 手动调试（在 Engine Pod 内）

```bash
# 模拟 preStop
bash $CONFIGMAP_PATH/prestop.sh

# 或单独跑 Python，可调参数
python3 $CONFIGMAP_PATH/prestop.py --max-wait 15 --poll-interval 3
```

## 参数

`prestop.py` 支持：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--max-wait` | 15 | 最长等待排空秒数 |
| `--poll-interval` | 3 | 轮询间隔秒数 |

## 依赖环境变量

| 变量 | 说明 |
|------|------|
| `POD_IP` | 本 Pod IP，用于访问 NodeManager |
| `CONFIGMAP_PATH` | ConfigMap 挂载路径（如 `/mnt/configmap`） |
| `CONFIG_PATH` | 运行时配置路径（如 `/usr/local/Ascend/Motor/conf`） |

NodeManager 端口从 `user_config.json` 读取，缺省为 **1026**。

## 相关 NodeManager 接口

| 接口 | 说明 |
|------|------|
| `POST /node-manager/pause` | PreStop 调用，返回非 headless 原生引擎的 `engine_metrics_targets` |
| `POST /node-manager/resume` | PreStop 取消时恢复（如滚动回滚） |

## 日志

输出重定向到 PID 1 的 stdout，可通过 `kubectl logs <pod>` 查看，关键字：

- `[prestop] Args:` / `[prestop] Exit code:`
- `Found NM PID`
- `active=`（每轮排队/运行请求数）
- `All requests drained` 或 `Timeout`
- `Sent SIGTERM to NM PID`

## 测试

```bash
bash tests/run_tests.sh tests/e2e/test_prestop_e2e.py
```

## Standalone Memcache 缩容数据迁移

`motor_controller_config.fault_tolerance_config.enable_kv_migration` 是启动总开关，默认 `false`。要在缩容时迁移 KV，需显式开启总开关和下述缩容开关，再重新部署。省略总开关时，preStop 只摘流并等待在途请求排空。开启总开关后，部署器把缩容开关写入 Pod 的 `MMC_ENABLE_KV_DRAIN_ON_SCALE_IN` 环境变量，供退出时使用。

在初次部署的 `kv_cache_store_config` 中设置 `backend: "memcache"`、`local_service_mode: "standalone"` 和 `enable_kv_drain_on_scale_in: true`。可通过 `drain_api_url` 配置 MetaService 的异步 drain 管理地址。迁移依赖支持指定目标与源 rank 注册代际校验的 Memcache 版本。

首次规划时，源 NodeManager 将剩余迁移时长传给 Controller。Controller 查询候选库存时为后续候选和数据搬迁预留时间。规划、提交和作业轮询共用该 Pod 的一次迁移预算。

跨节点 PCP/PP 缩容时，提供推理入口的 Pod 各自确认本机在途请求已排空。纯 headless Pod 在本机端点已暂停、同一实例所有入口都已确认后，按自己的存储节点标签提交作业。Controller 按实例和 Pod UID 暂存确认，入口先退出仍可复用。缺少确认或 Controller 切换导致确认丢失时，沿用原迁移超时。

批量缩容时，Controller 从 Kubernetes 读取当前服务的目标副本数和实际工作负载。某角色还有多余工作负载尚未开始删除时，暂缓选择该组目标；确认保留后，只允许属于保留工作负载且未进入删除的 Pod UID。开启主动迁移需重新应用部署器生成的 Controller 命名空间只读 Role 和 RoleBinding。无法确认保留目标时不会提交作业。

主动缩容沿用部署器的 Deployment 删除或 InferServiceSet 副本更新。kubelet 在终止时调用一次 preStop；preStop 完成原有的 pause 和请求排空后，通过独立的 `POST /node-manager/migration/prepare-exit` 提交跨实例迁移。若已有该源 Pod 的提交回执，复用原 drain ID。NodeManager 按该 ID 等待完成、失败或取消，preStop 收到终态即可退出。迁移预算复用 `kv_migration_timeout_sec`，默认 30 秒，独立于请求摘流的 `--max-wait`。预算耗尽后继续退出，并尽力取消已知作业。若 HTTP 回包丢失，preStop 会等到本地迁移预算到期。开启缩容迁移时，部署器将引擎 Pod 的退出宽限期设为至少 30 秒加迁移预算（向上取整），并保留更大的自定义值。调整时限后需重新部署 Pod。

若提交超时、返回 HTTP 500 或缺少有效的 `drain_id`，源 NodeManager 会保留 `operation.json`。用 `Submitting Memcache drain` 日志中的源 UID、操作号、目标标签、记录路径和提交时间核对 MetaService 作业日志。找到唯一对应的作业 ID 后，用 `GET /node-manager/migration/status?drain_id=<ID>` 查询。无法唯一匹配时，保留记录并将结果记为未知；删除记录并重发可能产生第二笔迁移。

明确失败且源 NM 已在提交锁内确认没有已有作业记录时，`prepare-exit` 返回包含源 UID 的 `drain_not_started` 错误，NM 先记录本次退出的拒绝结果，阻止该源后续新建迁移，preStop 随即非零结束。提交结果未知、无法识别的旧版错误或传输失败继续守住剩余迁移预算。
