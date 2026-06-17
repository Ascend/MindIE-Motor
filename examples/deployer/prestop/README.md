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
| `CONFIG_PATH` | 运行时配置路径（如 `/usr/local/Ascend/pyMotor/conf`） |

NodeManager 端口从 `user_config.json` 读取，缺省为 **1026**。

## 相关 NodeManager 接口

| 接口 | 说明 |
|------|------|
| `POST /node-manager/pause` | PreStop 调用，返回 `engine_mgmt_addrs` |
| `POST /node-manager/resume` | PreStop 取消时恢复（如滚动回滚） |

## 日志

输出重定向到 PID 1 的 stdout，可通过 `kubectl logs <pod>` 查看，关键字：

- `PRESTOP HOOK START` / `END`
- `Found NM PID`
- `active=`（每轮排队/运行请求数）
- `All requests drained` 或 `Timeout`
- `Sent SIGTERM to NM PID`

## 测试

```bash
python -m pytest tests/e2e/test_prestop_e2e.py -q
```
