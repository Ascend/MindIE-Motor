# Supported Features

<table style="table-layout: fixed; width: 1000px">
  <colgroup>
    <col style="width: 700px">
    <col style="width: 600px">
    <col style="width: 150px">
    <col style="width: 700px">
    <col style="width: 200px">
    <col style="width: 300px">
    <col style="width: 700px">
    <col style="width: 800px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">Feature Category</th>
      <th rowspan="2">Feature</th>
      <th rowspan="2">Status</th>
      <th colspan="2">Supported Inference Frameworks</th>
      <th colspan="2">Deployment Method</th>
      <th rowspan="2">Description</th>
    </tr>
    <tr>
      <th>vLLM Ascend</th>
      <th>SGLang</th>
      <th>K8s</th>
      <th>Docker only</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="6">Inference and Scheduling</td>
      <td>PD disaggregation</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./pd_disaggregation.md">Link</a></td>
    </tr>
    <tr>
      <td>EPD disaggregation</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./EPD_disaggregation.md">Link</a></td>
    </tr>
    <tr>
      <td>Dynamic bucket scheduling</td>
      <td>🔵</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./dynamic_bucket.md">Link</a></td>
    </tr>
    <tr>
      <td>KV cache affinity scheduling</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./kvcache_affinity.md">Link</a></td>
    </tr>
    <tr>
      <td>KV pooling capability deployment</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./kv_cache_store/README.md">Link</a></td>
    </tr>
    <tr>
      <td>max_tokens adaptation</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./max_tokens_adaptation.md">Link</a></td>
    </tr>
    <tr>
      <td rowspan="4">Scaling and Deployment</td>
      <td>Automatic elastic scaling</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./auto_scaling.md">Link</a></td>
    </tr>
    <tr>
      <td>Manual scaling</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./manual_scaling.md">Link</a></td>
    </tr>
    <tr>
      <td>Container snapshot</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./container_snapshot.md">Link</a></td>
    </tr>
    <tr>
      <td>D2D weight loading</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./startup_acceleration.md">Link</a></td>
    </tr>
    <tr>
      <td rowspan="7">Fault and Recovery</td>
      <td>Request rescheduling</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./fault_tolerance/rescheduler.md">Link</a></td>
    </tr>
    <tr>
      <td>Instance rescheduling</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./fault_tolerance/instance_rescheduling.md">Link</a></td>
    </tr>
    <tr>
      <td>Faulty instance restart</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>This feature takes effect by default</td>
    </tr>
    <tr>
      <td>ScaleP2D fault recovery</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./fault_tolerance/scale_p2d.md">Link</a></td>
    </tr>
    <tr>
      <td>Active/standby switchover feature</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./fault_tolerance/standby.md">Link</a></td>
    </tr>
    <tr>
      <td>Virtual inference health probe</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./sim_inference.md">Link</a></td>
    </tr>
    <tr>
      <td>Rate limiting</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./rate_limiting.md">Link</a></td>
    </tr>
    <tr>
      <td rowspan="3">Maintenance, Testing, and Security</td>
      <td>Precision detection feature</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./precision_detection.md">Link</a></td>
    </tr>
    <tr>
      <td>Tracing capability deployment</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./tracing.md">Link</a></td>
    </tr>
    <tr>
      <td>Data obfuscation inference</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./data_obfuscation.md">Link</a></td>
    </tr>
  </tbody>

</table>

**Features**:

- 🟢: Functioning normally; fully available and under continuous optimization.
- 🔵: POC; in the trial stage in the current version, not yet formally released.
- 🚧: Under development; being actively developed and will be supported soon.
- 🟡: Planned; planned for implementation in a later version.
- 🔴: Deprecated; sunset in the current version.

**Inference frameworks and deployment methods:**

- ✅: The inference framework and deployment method are supported.
- ❌: The inference framework and deployment method are not supported.
