# glm_5.2 · A2 · hybrid_multinode

来源：vLLM Ascend `releases/v0.30.0` `GLM5.2.md` / `GLM5.3-Flash.md` §5.1.2 A2（四机混部，DP4×TP8，每机 1 个 DP rank）。官方该页按 GLM-5.3 切分书写，GLM-5.2 共用同一套 A2 混部拓扑。

场景：A2 4-pod PD 混部。Motor 适配：不写 `api-server-count`、不写 `data-parallel-start-rank`（node0 由 Motor 推断 rank 0，其余 rank `--headless`）。`fuse_muls_add` 按原文写在 `ascend_compilation_config` 内。填写镜像与权重路径后，按仓库 deployer 指导部署。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
