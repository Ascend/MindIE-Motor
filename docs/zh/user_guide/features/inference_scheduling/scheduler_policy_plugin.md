# 自定义选点插件快速接入

本文介绍如何编写、安装并启用一个自定义实例/endpoint 选点插件。示例按候选 endpoint 当前负载从小到大排序；Motor 核心继续负责候选过滤、最终分配和负载释放。

## 环境准备

- Coordinator 已部署，并能连接可用的推理实例；Motor 版本支持选点插件 API v2。
- 已准备 `user_config.json`，以及 `pip`、`setuptools>=61` 和 `wheel`。
- 确认 Inference Worker 使用的 Python 解释器。本文的 `python` 命令均使用该解释器，插件也必须安装在同一环境。

插件只对已确定角色的候选池排序，不能切换混部或 PD 分离拓扑。显式指定实例和有效的会话亲和 pin 优先，不调用插件。

轮询、负载均衡、KV 缓存亲和和动态分桶以进程内插件随 Motor 交付，继续通过原有的 P/D 调度类型配置选择。以下步骤适用于独立打包的客户插件。

## 使用步骤

### 步骤 1：编写插件

创建一个最小工程：

```text
my_motor_policy/
├── pyproject.toml
└── src/
    └── my_motor_policy/
        └── __init__.py
```

在 `src/my_motor_policy/__init__.py` 中实现策略：

```python
from motor.coordinator.scheduler.policy.api import (
    EndpointSelectionPolicy,
    RankedCandidate,
    SelectionInput,
)


class LeastLoadPolicy(EndpointSelectionPolicy):
    api_version = 2

    def rank(self, selection: SelectionInput) -> list[RankedCandidate]:
        weight = float(self.options.get("instance_weight", 0.05))
        ranked = [
            RankedCandidate(
                id=candidate.id,
                score=candidate.active_tokens + weight * candidate.instance_active_tokens,
            )
            for candidate in selection.candidates
            if candidate.id not in selection.excluded
        ]
        return sorted(ranked, key=lambda item: (item.score, item.id.instance_id, item.id.endpoint_id))
```

`selection.candidates` 包含本次角色可用的候选快照；`active_tokens` 是 endpoint 负载，`instance_active_tokens` 是实例负载。`options` 来自后面的配置。上述示例分数越小优先级越高，并通过 ID 稳定打破同分情况。

接口要求：

- 继承抽象基类 `EndpointSelectionPolicy`，声明 `api_version = 2`，实现**同步** `rank()`。基类定义排序接口，插件决定采用哪种算法。
- 返回按优先级排列的非空 `RankedCandidate` 列表，或返回 `None` 放弃本次选点并使用内置策略。`score` 用于日志观测，Motor 取列表首项，CAS 冲突时使用刷新后的快照重新调用插件。
- 返回的 ID 必须来自本次候选，不能重复或属于 `selection.excluded`；分数必须是有限数值。
- 不要修改 Motor 状态、执行长时间阻塞操作或依赖跨 Worker 共享的插件对象。每个启用插件的 Inference Worker 按角色拥有独立插件进程，同一配置项覆盖多个角色时也不会共享实例。

请求长度、模型、用户和会话字段等可用数据及其限制见[设计文档的“客户插件接口”章节](../../../design/scheduler_policy_plugin.md#客户插件接口)。示例中的用户/会话元数据不是认证的租户身份。

### 步骤 2：注册并安装插件

在工程根目录的 `pyproject.toml` 中注册 Entry Point：

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "my-motor-policy"
version = "0.1.0"
requires-python = ">=3.10"

[project.entry-points."mindie_motor.scheduling_policies"]
"mycompany.least_load" = "my_motor_policy:LeastLoadPolicy"
"mycompany.prefill_load" = "my_motor_policy:LeastLoadPolicy"
"mycompany.decode_load" = "my_motor_policy:LeastLoadPolicy"

[tool.setuptools.packages.find]
where = ["src"]
```

等号左边的 `mycompany.least_load` 是配置中要填写的**插件名**；右边是 `Python 模块:类名`。同一 Python 环境中每个插件名必须唯一。这里注册三个入口名指向同一示例类，P/D 可以按名称分别配置；客户也可以让两个入口指向自己的不同实现。其他插件依赖应在包元数据中声明，并随部署环境准备好。

进入 `my_motor_policy/` 工程目录，构建并安装：

```bash
python -m pip wheel --no-deps --no-build-isolation . -w dist
python -m pip install dist/my_motor_policy-0.1.0-py3-none-any.whl
```

安装后可先检查 Entry Point 是否可被该解释器发现，并对策略做一次本地输入/输出验证：

```bash
python - <<'PY'
from importlib.metadata import entry_points
from motor.coordinator.scheduler.policy.api import (
    CandidateId, CandidateSnapshot, RequestContext, SelectionInput,
)

matches = [ep for ep in entry_points(group="mindie_motor.scheduling_policies")
           if ep.name == "mycompany.least_load"]
assert len(matches) == 1, f"期望一个插件入口，实际找到 {len(matches)} 个"
policy = matches[0].load()(options={"instance_weight": 0.05})
selection = SelectionInput(
    request=RequestContext("smoke-test", "prefill", "model", 10, 20),
    candidates=(
        CandidateSnapshot(CandidateId(1, 0), 20.0, 20.0, "10.0.0.1", "vllm"),
        CandidateSnapshot(CandidateId(2, 0), 5.0, 5.0, "10.0.0.2", "vllm"),
    ),
    excluded=frozenset(),
    attempt=0,
)
ranked = policy.rank(selection)
assert ranked and ranked[0].id == CandidateId(2, 0), ranked
print("Entry Point 加载成功；本地示例首选 instance=2, endpoint=0")
PY
```

该检查只验证包发现和策略输出；是否被运行中的 Worker 加载及实际选点，还需执行步骤 4。

#### 容器部署

对于 Docker/Kubernetes，将 wheel 及其依赖放入 **Coordinator 实际使用的镜像**，并安装到启动 Motor 的 Python 环境。下面是构建镜像时的示意；`<motor-python>` 需替换为基础镜像中 Motor 使用的解释器路径：

```dockerfile
FROM <现有的-motor-运行镜像>
COPY dist/my_motor_policy-0.1.0-py3-none-any.whl /tmp/my_motor_policy.whl
RUN <motor-python> -m pip install --no-deps /tmp/my_motor_policy.whl
```

确认构建阶段有权限向该 Python 环境安装包，并为所有运行 Inference Worker 的 Coordinator 副本使用包含插件的镜像。不要只在本机或单个临时容器安装：容器替换后包可能丢失。镜像更新与重启按现有部署方式执行。

### 步骤 3：配置并启用插件

将以下配置合并到现有 `user_config.json` 的 `motor_coordinator_config.scheduler_config` 中，保留文件中的其他配置：

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "prefill_scheduler_type": "load_balance",
      "decode_scheduler_type": "load_balance",
      "policy_plugin": [
        {
          "role": ["prefill"],
          "name": "mycompany.prefill_load",
          "options": {"instance_weight": 0.05},
          "timeout_ms": 100,
          "startup_timeout_ms": 5000,
          "cooldown_seconds": 30
        },
        {
          "role": ["decode"],
          "name": "mycompany.decode_load",
          "options": {"instance_weight": 0.2},
          "timeout_ms": 50,
          "startup_timeout_ms": 5000,
          "cooldown_seconds": 10
        }
      ]
    }
  }
}
```

| 配置 | 作用 |
|------|------|
| `role` | 必填非空数组，可选 `prefill`、`decode`、`union`、`encode`；不接受字符串，项内重复或与其他项重叠均报错 |
| `name` | 必填非空字符串，必须与 Entry Point 名称完全一致 |
| `options` | 传给该项各角色插件的构造函数，默认 `{}` |
| `timeout_ms` | 单次插件排序交换的等待预算，默认 50 毫秒，本例 P 为 100 毫秒 |
| `startup_timeout_ms` | 插件启动握手等待预算，默认 5000 毫秒 |
| `cooldown_seconds` | 该角色插件故障后后台恢复前的冷却时间，默认 30 秒 |

上例 P/D 使用不同入口名、参数和预算。若使用同名插件且配置相同，可合并：

```json
{
  "policy_plugin": [
    {"role": ["prefill", "decode"], "name": "mycompany.least_load", "options": {"instance_weight": 0.05}}
  ]
}
```

合并只复用配置；P/D 仍分别拥有插件实例、子进程、锁、冷却和指标。一方繁忙或失败不会占用另一方的插件执行通道。
也可以分两项使用相同 name 和不同 options。Union 不继承 Prefill 的插件配置：混部场景需显式加入 `"union"`；Encode 同理。

保留已有内置策略：`prefill_scheduler_type` 是 P/U/E 的默认和回退策略，`decode_scheduler_type` 是 D 的默认和回退策略。
角色未配置、插件繁忙、故障或超时时，使用该角色原配置的内置策略。删除 `policy_plugin` 或设为 `[]` 可禁用全部插件，不创建子进程。
配置成功后，客户插件接管该角色的候选排序。成功走客户插件时不会运行内置 KV 亲和查询或动态分桶，日志中的 `matched` 和 `hit_rate` 为 `None`。这些内置策略会在对应角色回退时执行。
每个 Worker 最多四个插件子进程。

旧草案的单对象 `policy_plugin: {"name": ..., "roles": [...]}` 会明确报迁移错误。
需改为外层列表，并把键 `roles` 改成 `role` 数组；旧 `["prefill", "union"]` 可以保留在新的一项中。
缺少 role/name、未知键、重复角色、空 role 数组或非法预算是配置错误，不能依靠运行时回退忽略。完整迁移示例见[设计文档](../../../design/scheduler_policy_plugin.md#旧单对象配置迁移)。

配置完成后，按现有部署方式重启所有相关 Inference Worker。修改插件包或配置后也需要重启，运行时不会自动发现新插件。

### 步骤 4：验证插件

1. **检查加载日志。** 查看每个 Inference Worker 的日志，确认 `Scheduling plugin loaded` 分别包含 `name=mycompany.prefill_load role=prefill` 和 `name=mycompany.decode_load role=decode`。子进程日志还会记录包名、版本和 Entry Point 来源。
2. **检查选点结果。** 向推理服务发送一条经过已启用角色的正常请求。在处理请求的 Worker 日志中查找 `scheduled`，确认 `policy` 显示对应插件名，并核对所选 instance/endpoint 是否符合预期。
3. **检查调用指标。** 将以下地址替换为可访问的 Worker metaserver 地址及端口，然后执行：

   ```bash
   WORKER_METASERVER_URL="http://127.0.0.1:12000"
   curl -fsS "$WORKER_METASERVER_URL/metrics" | grep -E '^motor_policy_(loads|calls|errors|timeouts|fallbacks)_total'
   ```

   默认端口为 `inference_workers_config.worker_metaserver_base_port + worker_index`，起始值为 `12000`。监听地址优先使用 `POD_IP`，否则使用 Coordinator 的监听地址；`127.0.0.1` 只是示例，不保证在容器网络中可访问。设置 `worker_metaserver_base_port=0` 会关闭该抓取口。

   `motor_policy_loads_total` 增加表示加载成功；`motor_policy_calls_total` 增加表示插件参与了排序。使用 `errors`、`timeouts` 和 `fallbacks` 检查故障或回退。

   查询 `motor_policy_fallbacks_by_reason_total` 可查看具体回退原因，例如 `busy` 表示该 Worker 的同角色插件正在执行另一次排序。

   指标按 Worker 和角色分别统计，Obs 不自动汇总。单次请求不一定落到正在查看的 Worker，应检查所有相关 Worker；pin 路径和未启用的角色不会调用插件。

## 常见问题

| 现象 | 检查和处理 |
|------|------------|
| 提示未找到入口或匹配多个入口 | 在实际 Worker 使用的 Python 环境运行步骤 2 的 Entry Point 检查；核对插件名和安装包，移除重复注册的包。 |
| 启动后只有内置策略 | 检查配置列表是否包含请求角色、`name` 是否注册、所有 Worker 是否安装了包，以及修改后是否已重启。显式 pin 和有效会话亲和会优先使用原路径。 |
| 日志提示插件失败或 `fallbacks` 增长 | 按 role/name 查找 `Scheduling plugin failed` 或 `Scheduling plugin fallback`；检查构造参数、异常、候选 ID、重复 ID、空结果和非有限分数。 |
| `timeouts` 增长 | 检查 `rank()` 是否阻塞、执行时间及 `timeout_ms`。Worker 在插件繁忙或冷却时也会回退；预算不是端到端请求的硬时限。 |
| 指标无法访问或 `calls` 为 0 | 检查对应 Worker 的 metaserver 是否启用、地址和端口是否可达；确认请求命中该 Worker 和启用的角色，且未走 pin。 |

插件在与 Worker 相同权限的子进程中执行，进程隔离用于故障回收，不是安全沙箱。接口、生命周期和完整回退语义见[候选选点插件设计](../../../design/scheduler_policy_plugin.md)。
