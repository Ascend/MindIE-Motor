# 日志与诊断文件目录

Motor 默认将计算单元的运行日志、诊断输出和缓存放在同一目录下，便于按服务、实例定位文件；有效的显式路径优先保留。各组件仍负责文件写入、格式、轮转和清理；本文介绍默认目录规则及查找方法。

## 1. 从哪里查找

使用默认根 `/root/ascend/log` 和配套日志挂载启动后，在**组件所在宿主机**查找：

```text
/root/ascend/log/<模型名>/<service_id>/
```

| 层级 | 含义 |
| --- | --- |
| `/root/ascend/log` | `env.json` 的 `motor_common_env.MOTOR_LOG_ROOT_PATH` 默认值，表示容器内公共根 |
| `<模型名>` | 部署配置解析出的模型名，通常为 `served_model_name`；未配置时可能回退到模型配置或权重路径 |
| `<service_id>` | 环境生成阶段按 `<job_id>_<北京时间YYYYMMDDhhmmss>` 生成的服务身份 |
| `<计算单元相对路径>` | 区分一个 NodeManager 及其管理的本地引擎，按部署形态组成，见第 2 节 |

下文将服务目录记为 **S**，计算单元目录记为 **U**：

```text
S = <MOTOR_LOG_ROOT_PATH>/<模型名>/<service_id>
U = S/<计算单元相对路径>
```

使用已有 `plog-path` hostPath 日志卷的标准 K8s 模板时，部署器根据 `motor_common_env.MOTOR_LOG_ROOT_PATH` 调整容器内挂载点，默认映射到宿主机同名目录。`infer_service_set` 和 `multi_deployment` 可通过各组件的 `logging_config.host_log_dir` 指定宿主机位置，显式值优先于同名映射；`single_container` 仍采用同名映射。部署快照缺少该卷时不会自动补建。

Docker 新建容器或一键创建时按同一公共根配置挂载；已有挂载覆盖日志根时沿用该挂载，否则补充到宿主机同名目录的挂载。已有容器的 `--start` 不会补建或调整挂载。

组件单独指定的路径若位于日志根之外，需要自行确认挂载；角色单独设置的日志根不参与自动挂载。多节点部署时，文件分布在各组件所在节点，同名服务目录不代表节点间共享存储。

## 2. 公共目录结构

各部署形态仅计算单元目录 U 不同，组件子目录使用相同结构：

| 部署形态 | U 的默认结构 | 示例 |
| --- | --- | --- |
| K8s 多容器：ISS、`multi_deployment` | `S/<Pod名>/<容器名>` | `S/pod-prefill/vllm/` |
| K8s `single_container` | `S/<Pod名>/<容器名>/<role-index>` | `S/pod-all/mindie-server/prefill-0/` |
| Docker only 多容器 | `S/<容器名>` | `S/engine-p/` |
| Docker only 单容器 | `S/<容器名>/<role-index>` | `S/all-in-one/prefill-0/` |

兼容旧入口时，若未提供 `COMPUTE_UNIT_LOG_SUBPATH`，直接使用 `U=S`；单容器仍按 `S/<role-index>` 隔离本地实例。省略该层后，不额外提供计算单元间的目录隔离。若缺少模型名或服务身份，启动脚本保留已有日志路径；直接启动 NodeManager 且未设置 `SERVICE_ENGINE_LOG_PATH` 时，也保留引擎原有日志配置。

以下展示默认派生路径。目录存在不代表对应功能已开启或已有文件；vLLM 与 SGLang 按所选引擎使用对应分支。

```text
S/
├── motor-control/
│   ├── controller/<hostname分组>/<日志文件>
│   └── coordinator/<hostname分组>/<日志文件>
└── <计算单元相对路径>/                       U
    ├── motor/<hostname分组>/<日志文件>       NodeManager 日志
    ├── cann/
    │   ├── work/
    │   │   ├── log/                         CANN plog
    │   │   ├── atrace/                      CANN trace
    │   │   └── FE/<pid>/fusion_result.json   GE 融合信息
    │   ├── cache/                           Ascend / GE 原生缓存子目录
    │   └── torchinductor/                   Inductor / Autofuse 编译产物
    ├── triton/.triton/{cache,dump,override}/
    ├── vllm/
    │   ├── vllm_ascend/                      引擎服务日志
    │   ├── xdg/cache/vllm/                   引擎缓存
    │   ├── xdg/config/vllm/                  引擎配置
    │   ├── runtime/config/runtime_config.json
    │   ├── runtime/report/                  精度监测报告
    │   ├── runtime/kv_cache/                精度监测 dump
    │   └── watchdog/                        卡死诊断输出
    └── sglang/profile/                      性能分析输出
```

### 2.1 文件用途与控制方式

表中的计算面路径均相对于 U。配置文件和缓存也是统一目录的一部分，清理时不能全部按普通日志处理。

| 内容 | 目录或文件 | 路径控制方式 | 产生条件与注意事项 |
| --- | --- | --- | --- |
| Motor 运行日志 | `motor/`；控制面使用 `S/motor-control/<role>/` | `MOTOR_LOG_PATH` | 按 Motor 日志配置输出，Logger 继续追加 hostname 分组 |
| CANN plog | `cann/work/log/` | `ASCEND_WORK_PATH` | Motor 不设置 `ASCEND_PROCESS_LOG_PATH`；用户已有该变量时按 CANN 原生优先级处理 |
| CANN trace | `cann/work/atrace/` | `ASCEND_WORK_PATH` | 按 CANN 原生轨迹记录机制产生 |
| GE 编译缓存 | `cann/cache/` | `ASCEND_CACHE_PATH` | 编译触发；专用 GE / ACL 配置仍按原生优先级生效 |
| GE 融合信息 | `cann/work/FE/<pid>/fusion_result.json` | `ASCEND_WORK_PATH` | 受 `ge.exportCompileStat` 等原生配置控制 |
| vLLM-Ascend 服务日志 | `vllm/vllm_ascend/` | `additional_config` JSON 中的 `ascend_log_path` | 未配置时补默认路径，有效显式路径保留；各引擎进程是否完整使用该路径取决于框架版本 |
| vLLM 缓存与配置 | `vllm/xdg/{cache,config}/vllm/` | `XDG_CACHE_HOME`、`XDG_CONFIG_HOME` | 引擎在 XDG 基目录下追加 `vllm`；专用 `VLLM_*` 路径仍有原生优先级 |
| 精度监测配置、报告、dump | `vllm/runtime/` | `runtime_config_path`、`runtime_report_dir`、`runtime_dump_dir` | 版本要求见下文；不生成配置文件内容，不主动开启检测、dump 或热更新 |
| vLLM Watchdog | `vllm/watchdog/` | `watchdog_config.dump_dir` | 版本要求见下文；有效显式非空路径保留，缺省或空值保持禁用，不自动补路径 |
| SGLang profiler | `sglang/profile/` | `SGLANG_TORCH_PROFILER_DIR` | 由原生 profiling 流程触发，请求中的 `output_dir` 可覆盖 |
| Triton 缓存、dump、配置输入 | `triton/.triton/` | `TRITON_HOME` | 组件追加 `.triton`；专用 `TRITON_*` 路径仍有原生优先级 |
| Inductor / Autofuse | `cann/torchinductor/` | `TORCHINDUCTOR_CACHE_DIR` | 编译触发；可包含 `autofused_*`、`.so`、`.bin` 等，最终二进制也可能跟随上层输出路径 |

公共路径环境变量遵循“未配置时使用默认路径，有效显式值优先保留”：包括 `MOTOR_LOG_PATH`、`ASCEND_WORK_PATH`、`ASCEND_CACHE_PATH`、`TORCHINDUCTOR_CACHE_DIR`、`XDG_CACHE_HOME`、`XDG_CONFIG_HOME`、`TRITON_HOME` 和 `SGLANG_TORCH_PROFILER_DIR`。显式路径偏离默认目录时输出告警。`VLLM_*`、`TRITON_*` 等专用路径仍按组件原有优先级生效。

vLLM 的 `ascend_log_path` 和通过版本判断的 runtime 三路径也保留有效显式值，并在偏离默认目录时告警；未配置的路径才由 Motor 补齐。Watchdog 有效非空 `dump_dir` 采用相同的保留与告警规则。显式路径的挂载、写入权限和多实例隔离由使用者保证，Motor 不自动为它们追加实例目录或调整挂载。

runtime 三路径要求 **`vllm-ascend >= 0.33.0`**；Watchdog 则独立要求 **`vllm >= 0.33.0`**。按 PEP 440 比较完整版本，例如 `0.33.0rc1`、`0.33.0.dev1` 低于门槛，而 `0.34.0rc1`、`0.34.0.dev1` 满足门槛。版本低于门槛或无法读取、解析时，Motor 不传入 runtime 三路径或整组 Watchdog 参数；用户已显式配置的对应项也会被省略，并输出告警。无法比较不代表已确认引擎不支持。

路径配置不会自动开启上述诊断功能，也不保证执行普通推理就产生 trace、融合信息、Autofuse、runtime 或 Watchdog 文件。初始化阶段可能只有空目录；runtime 配置文件需要由框架既有机制提供。

## 3. 与容器日志采集的关系

| 方式 | 数据来源 | 保存位置 |
| --- | --- | --- |
| 组件写入的日志、诊断文件和缓存 | Motor、引擎及依赖组件自行写文件 | 默认归一目录或显式指定路径；挂载覆盖时保存到宿主机对应目录 |
| `auto_log_collect` / `show_log.sh` | 跟踪采集 Pod 容器输出 | 部署端 `examples/deployer/log_collect/log/` 下 |

两条链路独立，组件写入的文件不会替代容器输出采集，也不会全部合并成一个日志。容器输出采集用法见[快速启动中的日志说明](../quick_start.md)。

未消费路径配置的引擎进程、路径初始化前的输出，以及其他旁路组件仍可能写入原生默认位置。例如部分旧版 vLLM-Ascend 的 API Server 会保留全局 `/root/ascend/log/vllm_ascend/` 日志；Motor 不事后搬移这些文件。定位问题时可结合原生目录与容器输出，按组件和发生时间核对。
