# Sample scheduling policy plugin

Install in the same Python environment as Coordinator Inference Workers:

```bash
cd examples/scheduler_plugin
python -m pip install -e .
```

`policy_plugin.name` 可写内置名（与 `scheduler_type` 等价）或 Entry Point 名称。Enable in `user_config.json`:

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "policy_plugin": {
        "name": "acme.weighted_tokens",
        "options": {
          "load_weight": 1.0,
          "kv_weight": 0.8
        },
        "fallback": "load_balance"
      }
    }
  }
}
```

Restart Coordinator after installing or changing the plugin.

Design reference: `docs/zh/design/scheduler_policy_plugin.md`.
