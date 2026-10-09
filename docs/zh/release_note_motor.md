
# 版本配套说明

## 产品版本信息

**表 1**  产品版本信息

| 项目 | 内容 |
| ---- | ---- |
| 产品名称 | MindIE Motor |
| 产品版本 | 3.2.0 |
| 版本类型 | 正式版本 |
| 维护周期 | 三个月 |

## 相关产品版本配套说明

**表 2**  版本配套表（Atlas 800I A2推理服务器/Atlas 800I A3超节点服务器）

| 产品名称 | 版本 |
| --- | --- |
| MindIE Motor | 3.1.0 |
| CANN | 9.0.1 |
| MindCluster | 26.1.0 |
| TorchNPU | 26.0.0 |
| vLLM | v0.23.0 |
| vLLM Ascend | releases/v0.23.0 |
| Mooncake | v0.3.11.post1 |
| CCAE  | iMaster CCAE V100R026C10SPC100 |
| Ascend HDK | 26.0.RC1 |

**表 3**  版本配套表（Ascend 950PR系列产品）

| 产品名称 | 版本 |
| --- | --- |
| MindIE Motor | 3.2.0 |
| CANN | 9.1.0 |
| MindCluster | 26.1.0 |
| TorchNPU | 26.1.0 |
| vLLM | v0.23.0 |
| vLLM Ascend | releases/v0.23.0 |
| Mooncake | v0.3.11.post1 |
| CCAE  | iMaster CCAE V100R026C10SPC100 |
| Ascend HDK | 26.0.RC1 |

## 版本兼容性说明

MindIE Motor与各组件需要配套使用，请勿跨版本混用各组件。

> [!NOTE]说明
> 下方表格中的“/”表示不配套，“Y”表示可配套。

**表 4**  MindIE Motor与CANN版本兼容

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE Motor</th>
      <th colspan="3">CANN版本</th>
    </tr>
    <tr>
      <th>9.0.0</th>
      <th>9.0.1</th>
      <th>9.1.0</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>3.0.0</td>
      <td>Y</td>
      <td>/</td>
      <td>/</td>
    </tr>
    <tr>
      <td>3.1.0</td>
      <td>Y</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
    <tr>
      <td>3.2.0</td>
      <td>/</td>
      <td>/</td>
      <td>Y</td>
    </tr>
  </tbody>

</table>

**表 5**  MindIE Motor与MindCluster版本兼容

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE Motor</th>
      <th colspan="2">MindCluster版本</th>
    </tr>
    <tr>
      <th>26.0.X</th>
      <th>26.1.X</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>3.0.0</td>
      <td>Y</td>
      <td>/</td>
    </tr>
    <tr>
      <td>3.1.0</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
    <tr>
      <td>3.2.0</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
  </tbody>

</table>

**表 6**  MindIE Motor与TorchNPU版本兼容

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE Motor</th>
      <th colspan="2">TorchNPU版本</th>
    </tr>
    <tr>
      <th>26.0.X</th>
      <th>26.1.X</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>3.0.0</td>
      <td>Y</td>
      <td>/</td>
    </tr>
    <tr>
      <td>3.1.0</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
    <tr>
      <td>3.2.0</td>
      <td>/</td>
      <td>Y</td>
    </tr>
  </tbody>

</table>

**表 7**  MindIE Motor与CCAE版本兼容

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE Motor</th>
      <th colspan="2">CCAE版本</th>
    </tr>
    <tr>
      <th>iMaster CCAE V100R026C00SPCXXX</th>
      <th>iMaster CCAE V100R026C10SPCXXX</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>3.0.0</td>
      <td>Y</td>
      <td>/</td>
    </tr>
    <tr>
      <td>3.1.0</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
    <tr>
      <td>3.2.0</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
  </tbody>

</table>

## 版本使用注意事项

- MindIE Motor 3.2.0 商发推荐配置为 CANN 9.1.0、TorchNPU 26.1.X、vLLM v0.26.0rc1、vLLM Ascend v0.26.0rc1，配套版本详见上方表 2。
- 从 MindIE Motor 3.1.0 升级时，建议同步将 CANN 升级至 9.1.0、TorchNPU 升级至 26.1.X、vLLM / vLLM Ascend 升级至 v0.26.0rc1 系列，与商发推荐配置保持一致。请勿跨商发大版本混用各组件，避免未验证的组合出现兼容性问题。
- 下方兼容性表中的 Y 表示该组合已通过 MindIE Motor 商发验证，"不配套"表示该组合未在本次商发范围内验证。

## 3.2.0更新说明

### 新增特性

- 支持长短序列动态分桶调度，在长短请求混合场景下降低TPOT、提升Decode输出吞吐（POC）。
- 支持SGLang推理引擎服务化后端，可对接SGLang引擎提供PD分离推理服务（POC）。
- 支持数据混淆推理（PMCC），Coordinator侧完成混淆词表与多模态张量的双向置换，可直接承载数据混淆权重。
- 支持D2D权重加载，新实例启动时可从集群内同角色就绪实例直接拉取权重，缩短实例启动时间。
- 支持max_tokens自适应，在路由前按实际输入token数裁剪输出上限，避免请求因超出模型上下文窗口被引擎拒绝。
- 支持Docker only场景一键部署推理服务，并新增基于Slurm + Apptainer的部署方式。
- 支持跨超节点部署服务、PD异构部署，以及推理大EP场景下DP域缩容运行。
- 新增容量规划指标，支持基于HPA的自动扩缩容与PD配比推导。
- 支持Prefill与Decode分别配置调度策略。
- Coordinator调度热路径完成Rust重构，调度指标通过共享内存暴露，提升调度性能与可观测性。
- 新增 standalone Coordinator 独立部署模式与 Mooncake standalone 部署适配。
- 支持 Ascend 950系列（A5）产品 IPv6 单栈组网下部署推理服务。
- 提供 DeepSeek V4.1 Flash、Kimi K3 等模型的专用配套镜像。
- 支持 vLLM StartPlan 与 graph reuse acceleration 能力。

### 修改特性

- Coordinator支持vLLM Render流式Token In/Token Out，并提供流式独立开关与启动参数透传能力。
- KV Cache亲和性调度支持阈值配置，并输出HBM/CPU/Disk命中tokens指标；DPLB调度增加KV usage加权。
- 容器快照特性支持部署配置自动生成。
- NodeManager适配MooncakeConnectorV2连接器。
- K8s场景Coordinator主备倒换后standby不再退出进程，提升控制面可靠性。

### 删除特性

无

### 接口变更说明

- 新增`/v1/responses`接口。
- `GET /instances`接口返回内容新增pool/healthy状态与熔断快照字段。
- 新增`motor:*`调度指标端点，通过共享内存对外暴露调度指标。

### 已解决的问题

无

### 遗留问题

无

## 升级影响

### 升级过程对现行系统的影响

- 对业务的影响

  软件版本升级过程中会导致业务中断。

- 对网络通信的影响

  对网络通信无影响。

### 升级后对现行系统的影响

- 对业务的影响

  对业务无影响。

- 对网络通信的影响

  对网络通信无影响。

## 漏洞修补列表

无
