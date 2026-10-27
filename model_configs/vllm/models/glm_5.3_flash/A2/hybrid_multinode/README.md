# glm_5.3_flash · A2 · hybrid_multinode

来源：vLLM Ascend `releases/v0.30.0` `GLM5.2.md` / `GLM5.3-Flash.md` `GLM5.3-Flash.md` §5.2 A2 两机混部（DP2×TP8，每机 1 个 DP rank）。

场景：A2 2-pod PD 混部。官方该脚本未开 `kv-cache-dtype` / prefix caching。Motor 适配：不写 `api-server-count`、不写 `data-parallel-start-rank`。填写镜像与权重路径后，按仓库 deployer 指导部署。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
