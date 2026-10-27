# glm_5.3_flash · A2 · pd

来源：vLLM Ascend `GLM5.3-Flash.md` §5.3 A2（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，Prefill 代码块起始行 987 / Decode 起始行 1120）。

场景：A2 4 机 1P1D（Prefill 2×8 卡 DP2×TP8，Decode 2×8 卡 DP8×TP2，`MooncakeConnectorV2`）。本目录与其他部署形态、硬件独立呈现。填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点：

- 不写 `api-server-count` / `--headless`（P1 由 Motor 按 rank 自动加 `--headless`）。
- `attention_config` 写成嵌套对象。不写 `nnodes`（PP1）。已去掉原文 profiler 字段。
- 该配置完成静态转换与字段校验，未执行 NPU 启动验证。
