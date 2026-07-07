# PD分离说明

---

## 什么是PD分离？

**PD 分离**（Prefill & Decode 分离）将大语言模型推理的预填充（Prefill）与解码（Decode）两个阶段拆分到不同实例上运行，适用于对时延和吞吐要求较高的场景。通过 PD 分离可提高 NPU 利用率，减轻 Prefill 与 Decode 分时复用带来的相互干扰，在相同时延下提升整体吞吐。

两个推理阶段的含义如下：

- **Prefill 阶段**：对输入 prompt 执行一次完整前向传播，生成初始隐藏状态（Hidden States），**计算密集型**；每个新输入序列都需执行一次 Prefill。
- **Decode 阶段**：基于 Prefill 结果逐步生成后续 token，每步仅计算最新 token 的激活与 attention，单步计算量较小，但需反复执行直至生成结束，**访存密集型**（以 KV Cache 等内存访问为主）。

本仓库采用**多机 PD 分离**部署方案：通过 K8s Service 为 Coordinator 暴露推理入口，使用多个 Deployment 分别部署 Controller（单 Pod）、Coordinator（单 Pod）以及 Server（P 实例与 D 实例各若干 Pod）。Controller 负责集群与实例管理，Coordinator 接收用户请求并调度至 P/D 实例，由 P 实例与 D 实例协同完成一次完整推理。

---

## PD 分离的主要优势有哪些？

- **资源利用更优**：Prefill 为计算密集型、Decode 为访存密集型，特性不同，分离部署可更充分利用 NPU 的计算与带宽资源。
- **吞吐能力提升**：Prefill 处理新请求的同时，Decode 可持续处理已有请求的解码，整体处理能力更高。
- **时延更可控**：两阶段分离可减少排队与等待，尤其在高并发场景下有助于降低时延。
