# MindIE Motor补丁

## 补丁应用场景

**为解决MindIE Motor仓与开源社区代码依赖的协同问题，对开源社区的代码修改使用补丁方式实现**。

## 补丁范围

| 补丁包名       | 补丁修复场景              |
| ------------ | ----------------- |
| **vllm_multi_connector.patch** | 修复vllm代码中layerwise叠加multi_connector方式无法推理问题 |

## 操作步骤

执行以下命令，将补丁应用到代码中。

```bash
python patch_apply.py
```

>说明: 拉起服务中指定的镜像需要提前完成了打补丁操作。
