# 特性支持

本页汇总 MindIE Motor 当前版本的特性支持情况。你可以按功能类别查询特性的可用状态，以及支持的推理引擎和部署方式。点击特性名称可查看使用详细的特性使用说明。

说明：POC 表示当前版本可试用，尚未正式商发。支持列中的「—」表示尚不支持。

<div id="feature-support" markdown="1">

## 推理与调度

本类特性主要包括PD分离和请求调度两大类，通过将Prefill实例和Decode实例分开部署，有助于提升计算资源利用效率；通过KV cache池化和亲和性调度，能够最大化复用KV cache，减少计算量。

<div class="feature-support-table">
<table>
  <colgroup>
    <col class="feature-name">
    <col class="feature-status">
  </colgroup>
  <colgroup span="2"></colgroup>
  <colgroup span="2"></colgroup>
  <thead>
    <tr>
      <th scope="col" rowspan="2">功能特性</th>
      <th scope="col" rowspan="2">状态</th>
      <th scope="colgroup" colspan="2">推理引擎</th>
      <th scope="colgroup" colspan="2">部署方式</th>
    </tr>
    <tr>
      <th scope="col">vLLM Ascend</th>
      <th scope="col">SGLang</th>
      <th scope="col">K8s</th>
      <th scope="col">Docker only</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="./inference_scheduling/pd_disaggregation.md">PD 分离</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./inference_scheduling/EPD_disaggregation.md">EPD 分离</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./inference_scheduling/dynamic_bucket.md">动态分桶调度</a></td>
      <td>POC</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./inference_scheduling/kvcache_affinity.md">KV Cache 亲和性调度</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./inference_scheduling/agent_hint.md">Agent Hint 会话亲和与调度</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./inference_scheduling/kv_cache_store/README.md">KV 池化能力部署</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./inference_scheduling/max_tokens_adaptation.md"><code>max_tokens</code> 自适应</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
  </tbody>
</table>
</div>

## 扩缩容与部署

本类特性可根据业务负载情况，自动开启阔缩容调整实例数量，也可以手动调整实例数，并通过容器快照或 D2D 权重加载缩短启动时间。

<div class="feature-support-table">
<table>
  <colgroup>
    <col class="feature-name">
    <col class="feature-status">
  </colgroup>
  <colgroup span="2"></colgroup>
  <colgroup span="2"></colgroup>
  <thead>
    <tr>
      <th scope="col" rowspan="2">功能特性</th>
      <th scope="col" rowspan="2">状态</th>
      <th scope="colgroup" colspan="2">推理引擎</th>
      <th scope="colgroup" colspan="2">部署方式</th>
    </tr>
    <tr>
      <th scope="col">vLLM Ascend</th>
      <th scope="col">SGLang</th>
      <th scope="col">K8s</th>
      <th scope="col">Docker only</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="./scaling_deployment/auto_scaling.md">自动弹性扩缩容</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>—</td>
    </tr>
    <tr>
      <td><a href="./scaling_deployment/manual_scaling.md">手动扩缩容</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>—</td>
    </tr>
    <tr>
      <td><a href="./scaling_deployment/container_snapshot.md">容器快照</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>—</td>
    </tr>
    <tr>
      <td><a href="./scaling_deployment/startup_acceleration.md">D2D 权重加载</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
  </tbody>
</table>
</div>

## 故障与恢复

本类特性用于在故障发生时恢复服务，可根据实际故障场景，进行实例重调度、故障引擎重启、主备倒换，提升服务可靠性；并可同时探测服务健康状态、控制请求流量。

<div class="feature-support-table">
<table>
  <colgroup>
    <col class="feature-name">
    <col class="feature-status">
  </colgroup>
  <colgroup span="2"></colgroup>
  <colgroup span="2"></colgroup>
  <thead>
    <tr>
      <th scope="col" rowspan="2">功能特性</th>
      <th scope="col" rowspan="2">状态</th>
      <th scope="colgroup" colspan="2">推理引擎</th>
      <th scope="colgroup" colspan="2">部署方式</th>
    </tr>
    <tr>
      <th scope="col">vLLM Ascend</th>
      <th scope="col">SGLang</th>
      <th scope="col">K8s</th>
      <th scope="col">Docker only</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="./fault_tolerance/request_rescheduler.md">请求重调度</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>—</td>
    </tr>
    <tr>
      <td><a href="./fault_tolerance/instance_rescheduling.md">实例重调度</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>—</td>
    </tr>
    <tr>
      <td><a href="./fault_tolerance/engine_relaunch.md">故障引擎重启（默认生效）</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./fault_tolerance/scale_p2d.md">ScaleP2D 故障恢复</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>—</td>
    </tr>
    <tr>
      <td><a href="./fault_tolerance/dp_scale_down.md">DP 域缩容</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>—</td>
    </tr>
    <tr>
      <td><a href="./fault_tolerance/standby.md">主备倒换特性</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td><a href="../deployment/docker/ha.md">支持</a></td>
    </tr>
    <tr>
      <td><a href="./fault_tolerance/sim_inference.md">虚推健康探测</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./fault_tolerance/rate_limiting.md">服务限流</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
  </tbody>
</table>
</div>

## 维测与安全

本类特性用于检查推理精度、追踪请求链路，以及在推理过程中对数据进行混淆处理，帮助定位运行问题和保护数据。

<div class="feature-support-table">
<table>
  <colgroup>
    <col class="feature-name">
    <col class="feature-status">
  </colgroup>
  <colgroup span="2"></colgroup>
  <colgroup span="2"></colgroup>
  <thead>
    <tr>
      <th scope="col" rowspan="2">功能特性</th>
      <th scope="col" rowspan="2">状态</th>
      <th scope="colgroup" colspan="2">推理引擎</th>
      <th scope="colgroup" colspan="2">部署方式</th>
    </tr>
    <tr>
      <th scope="col">vLLM Ascend</th>
      <th scope="col">SGLang</th>
      <th scope="col">K8s</th>
      <th scope="col">Docker only</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="./maintenance_testing_security/precision_detection.md">精度检测特性</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./maintenance_testing_security/tracing.md">Tracing 能力部署</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
    <tr>
      <td><a href="./maintenance_testing_security/data_obfuscation.md">数据混淆推理</a></td>
      <td>可用</td>
      <td>支持</td>
      <td>—</td>
      <td>支持</td>
      <td>支持</td>
    </tr>
  </tbody>
</table>
</div>

</div>
