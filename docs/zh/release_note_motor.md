
# 版本配套说明

## 产品版本信息

**表 1**  产品版本信息

| 项目 | 内容 |
| ---- | ---- |
| 产品名称 | MindIE Motor |
| 产品版本 | 3.2.0 |
| 版本类型 | Release版本 |
| 维护周期 | 三个月 |

## 相关产品版本配套说明

**表 2**  版本配套表

| 产品名称 | 版本 |
| --- | --- |
| MindIE Motor | 3.2.0 |
| MindCluster | 26.2.0 |
| vLLM | v0.30.0 |
| vLLM Ascend | releases/v0.30.0 |
| Ascend HDK | 26.2.0 |

## 版本兼容性说明

MindIE Motor与各组件需要配套使用，请勿跨版本混用各组件。

> [!NOTE]说明
> 下方表格中的“/”表示不配套，“Y”表示可配套。

**表 3**  MindIE Motor与MindCluster版本兼容

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
      <th colspan="3">MindCluster版本</th>
    </tr>
    <tr>
      <th>26.0.X</th>
      <th>26.1.X</th>
      <th>26.2.X</th>
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
      <td>/</td>
    </tr>
    <tr>
      <td>3.2.0</td>
      <td>Y</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
  </tbody>

</table>

**表 4**  MindIE Motor与vLLM Ascend版本兼容

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
      <th colspan="3">vLLM Ascend版本</th>
    </tr>
    <tr>
      <th>releases/v0.18.0</th>
      <th>releases/v0.23.0</th>
      <th>releases/v0.30.0</th>
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
      <td>/</td>
    </tr>
    <tr>
      <td>3.2.0</td>
      <td>Y</td>
      <td>Y</td>
      <td>Y</td>
    </tr>
  </tbody>

</table>

## 版本使用注意事项

无

## 3.2.0更新说明

### 新增特性

- 支持SGLang推理引擎服务化后端，可对接SGLang引擎提供PD分离推理服务。
- 支持D2D权重加载，新实例启动时可从集群内同角色就绪实例直接拉取权重，缩短实例启动时间。
- 支持max_tokens自适应，在路由前按实际输入token数裁剪输出上限，避免请求因超出模型上下文窗口被引擎拒绝。
- 支持Docker only场景一键部署推理服务，并新增基于Slurm + Apptainer的部署方式。
- 支持跨超节点部署服务、PD异构部署，以及推理大EP场景下DP域缩容运行。
- 新增容量规划指标，支持基于HPA的自动扩缩容与PD配比推导。
- 支持Prefill与Decode分别配置调度策略。
- 新增 Coordinator 独立部署模式与 Mooncake standalone 部署适配。
- 支持 Ascend 950PR&950DT 系列产品 IPv6 单栈组网下部署推理服务。
- 推理引擎服务启动加速。
- Coordinator支持独立的vLLM Render实现Tokenizer能力的服务。
- 支持数据混淆推理（PMCC），Coordinator侧完成混淆词表与多模态张量的双向置换，可直接承载数据混淆权重。
- 支持长短序列动态分桶调度，在长短请求混合场景下降低TPOT、提升Decode输出吞吐（POC）。

### 修改特性

- KV Cache亲和性调度能力增强，支持HBM/CPU/Disk的命中；DPLB调度增加KV usage加权（POC）。
- 安全增强，缺省要求开启TLS加密通信。

### 删除特性

- 删除EngineServer模块，与推理引擎解耦。

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

## 病毒扫描及漏洞修补列表

### 病毒扫描结果

通过病毒扫描，未发现病毒。

### 漏洞修补列表

无

## 修订记录

| 文档版本 | 发布日期 | 修改说明 |
|--|--|--|
| 01 | 2026-10-15 | 第一次正式发布。 |
