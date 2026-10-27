# glm_5.2 · A2 · pd

来源：vLLM Ascend `GLM5.2.md` §5.1.3 A2 八机 PD（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，Prefill 代码块起始行 2045 / Decode 起始行 2144）。原文该页按 GLM-5.3 切分书写，GLM-5.2 共用同一套 A2 PD 拓扑；PP 分区按 GLM-5.2 使用 `41,37`。

场景：A2 8-pod PD（Prefill 4×8 卡 DP1×TP16×PP2，`nnodes=4`；Decode 4×8 卡 DP16×TP2）。相对 A3 16 卡/节点，A2 为 8 卡/节点，同规模切分需要把每角色 Pod 数从 2 增到 4。本目录与其他部署形态、硬件独立呈现。填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点：

- Prefill 写 `nnodes=4` 与 `master-port`；Decode **不要**写 `nnodes`。
- `attention_config` 写成嵌套对象。不写 `api-server-count`（Motor 给非 0 rank 自动 `--headless`）。
- 不写 `fuse_muls_add`。
