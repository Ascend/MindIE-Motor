# 特性支持列表

<table style="table-layout: fixed; width: 1000px">
  <colgroup>
    <col style="width: 300px">
    <col style="width: 300px">
    <col style="width: 100px">
    <col style="width: 500px">
    <col style="width: 200px">
    <col style="width: 300px">
    <col style="width: 500px">
    <col style="width: 800px">
  </colgroup>
  <thead>
    <tr>
      <th rowspan="2">特性分类</th>
      <th rowspan="2">功能特性</th>
      <th rowspan="2">状态</th>
      <th colspan="2">支持的推理框架</th>
      <th colspan="2">部署方式</th>
      <th rowspan="2">说明</th>
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
      <td rowspan="6">推理与调度</td>
      <td>PD分离</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./pd_disaggregation.md">链接</a></td>
    </tr>
    <tr>
      <td>EPD分离</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./EPD_disaggregation.md">链接</a></td>
    </tr>
    <tr>
      <td>动态分桶调度</td>
      <td>🔵</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./dynamic_bucket.md">链接</a></td>
    </tr>
    <tr>
      <td>KV Cache亲和性调度</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./kvcache_affinity.md">链接</a></td>
    </tr>
    <tr>
      <td>KV池化能力部署</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./kv_cache_store/README.md">链接</a></td>
    </tr>
    <tr>
      <td>max_tokens自适应</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./max_tokens_adaptation.md">链接</a></td>
    </tr>
    <tr>
      <td rowspan="4">扩缩容与部署</td>
      <td>自动弹性扩缩容</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./auto_scaling.md">链接</a></td>
    </tr>
    <tr>
      <td>手动扩缩容</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./manual_scaling.md">链接</a></td>
    </tr>
    <tr>
      <td>容器快照</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./container_snapshot.md">链接</a></td>
    </tr>
    <tr>
      <td>D2D 权重加载</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./startup_acceleration.md">链接</a></td>
    </tr>
    <tr>
      <td rowspan="6">故障与恢复</td>
      <td>请求重调度</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./fault_tolerance/rescheduler.md">链接</a></td>
    </tr>
    <tr>
      <td>故障实例重启</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>该特性默认生效</td>
    </tr>
    <tr>
      <td>ScaleP2D故障恢复</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./fault_tolerance/scale_p2d.md">链接</a></td>
    </tr>
    <tr>
      <td>主备倒换特性</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>❌</td>
      <td><a href="./fault_tolerance/standby.md">链接</a></td>
    </tr>
    <tr>
      <td>虚推健康探测</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./sim_inference.md">链接</a></td>
    </tr>
    <tr>
      <td>服务限流</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./sim_inference.md">链接</a></td>
    </tr>
    <tr>
      <td rowspan="3">维测与安全</td>
      <td>精度检测特性</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./precision_detection.md">链接</a></td>
    </tr>
    <tr>
      <td>Tracing能力部署</td>
      <td>🟢</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./tracing.md">链接</a></td>
    </tr>
    <tr>
      <td>数据混淆推理</td>
      <td>🟢</td>
      <td>✅</td>
      <td>❌</td>
      <td>✅</td>
      <td>✅</td>
      <td><a href="./data_obfuscation.md">链接</a></td>
    </tr>
  </tbody>

</table>

**功能特性**：

- 🟢：功能正常；完全可用，持续优化中。
- 🔵：POC；当前版本为试用阶段，还未正式商发。
- 🚧：开发中；正在积极开发，即将支持。
- 🟡：已计划；计划后续版本实现。
- 🔴：已弃用；当前版本已日落。

**推理框架和部署方式：**

- ✅：支持该推理框架和部署方式。
- ❌：不支持该推理框架和部署方式。
