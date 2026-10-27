# glm_5.3 · A5 · hybrid_multinode

来源：vLLM Ascend `GLM5.2.md`（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，代码块起始行 371）；Motor 转换器基线 `eb35a376f48023f112ea37f52077da69f0a20e82`。

场景：950DT multi-node colocated; GLM-5.3 checkpoint。本目录与其他部署形态、硬件和上下文变体独立呈现，不可跨硬件套用参数。填写镜像与权重路径后，按仓库 deployer 指导部署。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
