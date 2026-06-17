# PD 混部部署示例

## 目录说明

本目录提供 vLLM PD 混部部署示例配置：

- `user_config.json`：PD 混部 `user_config` 示例（使用 `motor_engine_union_config` 与 `hybrid_*` 字段）
- `env.json`：部署环境变量示例（包含 `motor_engine_union_env`）

完整部署流程、配置项说明和故障排查请参考 [PD 混部服务部署](../../../../docs/zh/user_guide/deployment/k8s/pd_aggregation_deployment.md)。如需启用 KV Cache 亲和调度，在同一 `user_config.json` 中按 [KV Cache 亲和部署](../../../../docs/zh/user_guide/features/kvcache_affinity.md) 的 PD 混部说明修改配置即可。

## 使用方式

在 `examples/deployer` 目录执行：

```bash
cd examples/deployer

# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm/pd_hybrid

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/pd_hybrid/user_config.json --env_config_path ../infer_engines/vllm/pd_hybrid/env.json
```

如需仅检查 YAML 生成，可加 `--dry-run`。
