
# Version Matching Description

## Product Version Information

**Table 1** Product version information

| Project | Content |
| ---- | ---- |
| Product Name | MindIE Motor |
| Product Version | 3.2.0 |
| Version Type | Release version |
| Maintenance Cycle | Three months |

## Version Matching of Related Products

**Table 2** Version matching table

| Product Name | Version |
| --- | --- |
| MindIE Motor | 3.2.0 |
| MindCluster | 26.2.0 |
| vLLM | v0.30.0 |
| vLLM Ascend | releases/v0.30.0 |
| Ascend HDK | 26.2.0 |

## Version Compatibility Notes

MindIE Motor must be used with matching versions of each component. Do not mix components across different versions.

> [!NOTE]NOTE
> In the following tables, "/" indicates that the versions are not compatible, and "Y" indicates that they are compatible.

**Table 3**  Version compatibility between MindIE Motor and MindCluster

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
      <th colspan="3">MindCluster Version</th>
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

**Table 4**  Version compatibility between MindIE Motor and vLLM Ascend

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
      <th colspan="3">vLLM Ascend Version</th>
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

## Version Usage Notes

None

## 3.2.0 Update Notes

### New Features

- Added support for the SGLang inference engine service backend, which can interconnect with the SGLang engine to provide PD disaggregation inference services.
- Added support for D2D weight loading. When a new instance starts, it can pull weights directly from ready instances of the same role in the cluster, shortening the instance startup time.
- Added support for max_tokens adaptation, which trims the output upper limit based on the actual number of input tokens before routing, preventing requests from being rejected by the engine because they exceed the model context window.
- Added support for one-click deployment of inference services in Docker-only scenarios, and added a deployment mode based on Slurm + Apptainer.
- Added support for cross-SuperPoD service deployment, heterogeneous PD deployment, and DP domain scale-in in large-scale EP inference scenarios.
- Added capacity planning metrics, supporting HPA-based automatic scaling and PD ratio derivation.
- Added support for configuring scheduling policies for Prefill and Decode separately.
- Added the Coordinator independent deployment mode and Mooncake standalone deployment adaptation.
- Added support for deploying inference services in IPv6 single-stack networking on 950PR&950D products.
- Added startup acceleration for inference engine services.
- Added support for the Coordinator to use an independent vLLM Render service to implement tokenizer capabilities.
- Added support for data obfuscation inference (PMCC). The Coordinator side performs bidirectional permutation of the obfuscated vocabulary and multimodal tensors, and can directly host data obfuscation weights.
- Added support for dynamic bucket scheduling of long and short sequences, which reduces TPOT and improves Decode output throughput in mixed long and short request scenarios (POC).

### Modified Features

- Enhanced KV Cache affinity scheduling capabilities, supporting HBM/CPU/Disk hits; DPLB scheduling adds KV usage weighting (POC).
- Security enhancement: TLS encrypted communication is required by default.

### Removed Features

- Removed the EngineServer module to decouple from the inference engine.

### Interface Change Description

- Added the `/v1/responses` interface.
- The response content of the `GET /instances` interface adds the pool/healthy status and circuit breaker snapshot fields.
- Added the `motor:*` scheduling metric endpoint, which exposes scheduling metrics externally through shared memory.

### Resolved Issues

None

### Known Issues

None

## Upgrade Impact

### Impact on the Current System During the Upgrade

- Impact on services

  Service interruption occurs during the software version upgrade.

- Impact on network communication

  No impact on network communication.

### Impact on the Current System After the Upgrade

- Impact on services

  No impact on services.

- Impact on network communication

  No impact on network communication.

## Virus Scan and Vulnerability Patch List

### Virus Scan Result

No virus is found through the virus scan.

### Vulnerability Patch List

None

## Revision History

| Document Version | Release Date | Change Description |
|--|--|--|
| 01 | 2026-10-15 | First official release. |
