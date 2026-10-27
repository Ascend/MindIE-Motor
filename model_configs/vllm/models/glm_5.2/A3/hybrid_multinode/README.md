# glm_5.2 · A3 · hybrid_multinode

来源：vLLM Ascend `releases/v0.30.0` `GLM5.2.md` / `GLM5.3-Flash.md` §5.1.2 A3（两机混部，DP8×TP4，每机 4 个 DP rank）。官方该页按 GLM-5.3 切分书写，GLM-5.2 共用同一套 A3 混部拓扑。#1268 仅有 A3 短上下文 `w8a8c8_hybrid_single`，本目录补全上下文 202752 的两机混部。

场景：A3 2-pod PD 混部。Motor 适配：不写 `api-server-count`、不写 `data-parallel-start-rank`。填写镜像与权重路径后，按仓库 deployer 指导部署。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
