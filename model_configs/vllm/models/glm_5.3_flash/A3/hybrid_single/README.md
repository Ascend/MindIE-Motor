# glm_5.3_flash · A3 · hybrid_single

来源：vLLM Ascend `releases/v0.30.0` `GLM5.2.md` / `GLM5.3-Flash.md` `GLM5.3-Flash.md` §5.1 A3 单机混部（DP1×TP16）。

场景：A3 1-pod PD 混部，`kv-cache-dtype=int8`，MTP=5（`enforce_eager=false`）。填写镜像与权重路径后，按仓库 deployer 指导部署。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
