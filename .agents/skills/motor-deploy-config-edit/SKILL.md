---
name: motor-deploy-config-edit
description: Explicit atomic workflow under motor-deploy that translates deployment intent into a native user_config.json and env.json proposal. Use for Motor 配置生成、配置修改或 Prefill/Decode 部署意图; server validation belongs to motor-deploy-configure.
---

# Motor config editing framework

将用户意图映射为 Motor 原生 `user_config.json` + `env.json`。本 Skill 当前只提供保守
框架：不部署、不 dry-run、不创建 workspace profile，不自动扩充 tracked 映射表。

1. 读取用户指定配置或 `examples/infer_engines/<engine>/` 中与 deploy mode 匹配的模板。
2. 字段以当前 `docs/zh/user_guide/configuration/config_reference.md`、
   `examples/features/config_sample.json` 和 `motor/config/` 为权威；搜不到就停止，不猜。
3. 首次缺失 image、model path、served model、hardware、job ID 等关键值时询问用户。
4. 在用户指定的独立输出目录复制完整配置后做最小修改，不改模板原件，不默认写入
   tracked `examples/`。
5. 检查 P/D model/served model、KV role/port、parallel size/NPU、image、mount、job ID
   和必要 env 的一致性；只把静态检查称为 static validation。
6. 展示输出绝对路径、字段 diff、每个字段的源码/文档出处和未验证项。

配置写入需要用户明确同意目标目录和变更。后续交给 `motor-deploy-configure` 做原生
deployer dry-run；本 Skill 不把静态检查冒充可部署证明。
