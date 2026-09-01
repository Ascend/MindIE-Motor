# Version Matching Description

## Product Version Information

**Table 1** Product version information

| Project | Content |
| ---- | ---- |
| Product Name | MindIE Motor |
| Product Version | 3.1.0 |
| Version Type | Official release |
| Maintenance Cycle | Three months |

## Version Matching of Related Products

**Table 2** Version matching table (Atlas 800I A2 Inference Server/Atlas 800I A3 SuperPoD Server)

| Product Name | Version |
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

**Table 3** Version matching table (Atlas 850 SuperPoD Server)

| Product Name | Version |
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

## Version Compatibility Notes

MindIE Motor must be used with matching versions of each component. Do not mix components across different versions.

> [!NOTE]NOTE
> In the following tables, "/" indicates that the versions are not compatible, and "Y" indicates that they are compatible.

**Table 4**  Version compatibility between MindIE Motor and CANN

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE</th>
      <th colspan="3">CANN Version</th>
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

**Table 5**  Version compatibility between MindIE Motor and MindCluster

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE</th>
      <th colspan="2">MindCluster Version</th>
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

**Table 6**  Version compatibility between MindIE Motor and TorchNPU

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE</th>
      <th colspan="2">TorchNPU Version</th>
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

**Table 7**  Version compatibility between MindIE Motor and CCAE

<table style="table-layout: fixed; width: 750px">
  <colgroup>
    <col style="width: 150px">
    <col style="width: 150px">
    <col style="width: 150px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">MindIE</th>
      <th colspan="2">CCAE Version</th>
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

## Version Usage Notes

None

## 3.1.0 Update Notes

Starting from version 3.1.0, the original MindIE PyMotor code repository is renamed to MindIE Motor, and this naming will be used in subsequent versions. Its software positioning and basic functions remain unchanged, and it is compatible with the vLLM-Ascend inference engine.

### New Features

- Added support for Coordinator to define custom flow control specifications based on request fields, enabling tailored rate limiting.
- Added visualization support for inference metrics, along with fault request status tracking and performance anomaly event logging in link tracing.
- Added support for deploying inference services on the Ascend 950 products.  
- Added support for deploying PD co-location services.  
- Added support for configuring `cp`/`pp` fields during service deployment.  
- Added support for automatically scaling inference instances up or down based on business traffic intensity.

### Modified Features

- Added support for combining KV-affinity scheduling with Function Call features.  
- Enabled vLLM inference service deployment on Atlas 800I A3 SuperPoD Server and IPv6 single-stack environments.

### Removed Features

None

### Interface Change Description

None

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

## Vulnerability Patch List

None
