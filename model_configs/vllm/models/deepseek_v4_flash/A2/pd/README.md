# deepseek_v4_flash · A2 · pd

由 A3 现场跑通的 MTP 1P1D 按 **16 卡 = 两台 A2（各 8 卡）** 映射：引擎切分不变（Prefill DP4×TP4，Decode DP16×TP1，`mtp` tokens=1，`block-size=32`，1M 上下文），部署改为每个实例 2 个 8 卡 Pod。未在 A2 机器上实测。跨机互联与 A3 机内 HCCS 不同，属于映射残留风险。
