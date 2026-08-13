
# 版本配套说明

## 产品版本信息

**表 1**  产品版本信息

| 项目 | 内容 |
| ---- | ---- |
| 产品名称 | MindIE Motor |
| 产品版本 | 3.1.0 |
| 版本类型 | 正式版本 |
| 维护周期 | 三个月 |

## 相关产品版本配套说明

**表 2**  版本配套表（Atlas 800I A2 推理服务器/Atlas 800I A3 超节点服务器）

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

**表 3**  版本配套表（Atlas 850 超节点服务器）

| 产品名称 | 版本 |
| --- | --- |
| MindIE Motor | 3.1.0 |
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
  </tbody>

</table>

## 版本使用注意事项

无

## 3.1.0更新说明

原 MindIE PyMotor 代码仓自 3.1.0 版本起更名为 MindIE Motor，后续版本将沿用该命名。其软件定位与基本功能保持不变，兼容 vLLM-Ascend 推理引擎。

### 新增特性

- 支持Coordinator根据请求字段自定义流控规格，实现定制化流控。
- 支持推理指标可视化展示，在链路追踪中补充故障请求状态跟踪及性能异常事件记录。
- 支持在Ascend 950系列产品部署推理服务。
- 支持部署PD混部服务。
- 支持部署服务时配置cp/pp字段。
- 支持基于业务繁忙程度自动增加或减少推理实例数量。

### 修改特性

- 支持KV亲和性调度特性与Function Call特性叠加。
- 支持Atlas 800I A3 超节点服务器、IPv6单栈场景下部署vLLM推理服务。

### 删除特性

无

### 接口变更说明

无

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
