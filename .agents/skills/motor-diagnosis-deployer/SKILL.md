---
name: motor-diagnosis-deployer
description: Internal specialist used only after motor-diagnosis-startup proves the current evidence episode contains a deploy.py-owned defect. Diagnose valid input mishandled by native deploy.py argument handling, template/manifest generation, deployment-plan generation, or apply construction. Mere config drift, unsupported working directories, interpreter/dependency/bootstrap failures, incomplete checkouts/packages, missing local assets, platform rejection, post-apply symptoms, or a deploy.py failure alone remain outside this Skill.
---

# Motor deployer diagnosis

## Entry guard

Continue only when `motor-diagnosis-startup` recorded `category: deployer`, the current evidence episode, and evidence that identifies a native `deploy.py` transition as the owner of valid-input mishandling. Otherwise stop and return to startup classification. A failed `deploy.py`, traceback, missing YAML, API rejection, or observed config-chain drift is only a candidate symptom and cannot activate this Skill by itself.

Within the confirmed deployer category, find the last confirmed handoff and first failed contract for the same attempt. Failure location and cause ownership are separate: a failure may surface during deployment while its cause remains config or environment and therefore outside this Skill.

Treat unexplained `config→YAML→ConfigMap→Pod` drift as `config`, not deployer. Enter this Skill only after same-episode evidence ties that drift to a specific native `deploy.py` argument, translation, rendering, planning, or apply-construction defect.

Establish the matching revision's supported invocation contract before attributing path resolution: exact command form, working directory, entrypoint, required local files, interpreter, and dependencies. An unsupported invocation is `environment/bootstrap`; an unresolved contract returns `unknown` to startup. Classify path resolution as deployer only when the invocation is supported and native `deploy.py` still resolves its own required asset incorrectly.

## Input and evidence

Before retrying, collect:

- exact command, working directory, UTC window, exit status, and complete stdout/stderr or `.deploy.log`;
- config paths and hashes, generated-file inventory, endpoint/context/namespace;
- the actual remote Deployer source and Motor revision or `deploy.py` hash. Use local source only when provenance matches; do not build a replacement wrapper.

Collect only evidence available at the reached boundary:

- Before manifest generation, inspect command output, runtime/bootstrap facts, implicated config, and matching source.
- After generation, record manifest names, mtimes, and hashes; parse YAML and compare implicated values with native config.
- After apply begins, preserve the exact operation and API response, then inspect submitted objects and time-windowed Events.
- Reuse existing server-side dry-run evidence; do not regenerate it for diagnosis.

Redact secrets. Correlate by time, config hash, namespace, and object identity; never mix stale `output_yamls/` or another retry. Missing Events or live objects support a conclusion but do not prove no API request occurred.

## Diagnostic transitions

Find the last confirmed handoff and first failed contract for the same attempt. These checkpoints locate evidence; they do not expand Deployer ownership.

| Transition | In scope only when |
|---|---|
| Supported invocation and valid native input → deployment intent | Native `deploy.py` mishandles argument precedence, defaults, or normalization, or builds intent inconsistent with valid input |
| Deployment intent → generated manifest | Native `deploy.py` selects, translates, renders, or generates an incorrect or malformed manifest |
| Generated manifest → apply/submission operation | Native `deploy.py` constructs the wrong target, object, operation, or ordering, or submits an object inconsistent with confirmed valid intent |

Use the deepest directly proven handoff. A caught exception followed by success is not failure; if the next contract is unobservable, return `unknown`.

## Exit and exclusions

- Exit this Skill once the Kubernetes API accepts all required objects. Reconciliation, scheduling, Pod/Service/Endpoint readiness, config propagation, and runtime-code after acceptance belong elsewhere.
- Rejection of a valid object by RBAC, admission, or the platform is `environment`, not deployer.
- Unsupported working directory, interpreter/dependency/bootstrap failure, incomplete checkout/package, or unavailable required local file is outside this Skill regardless of where it surfaces.
- Invalid requested intent or native config is `config`. Unexplained config-chain drift remains `config` until same-episode evidence identifies a native `deploy.py` transition as the owner.
- A `deploy.py` wait timeout after API acceptance is not a Deployer root cause.

If evidence contradicts the startup classification, return `outside-scope` with `category: config` or the corrected category evidence rather than continuing or loading another child. Use `confirmed` for a complete causal chain, `probable` when one bounded link is missing, and `unknown` when deployer causes cannot be distinguished. For a code-path cause, report the redacted valid input → incorrect output relationship and exact source file:line only when proven.

## Output

Use exactly these sections and fields:

```markdown
## 结论

{用户可读阶段}阶段出错。<br>
具体原因：{直接根因；证据不足时写“证据不足，暂未定位”}。<br>
置信度：{高/中/低}。

## 证据链

- {决定性命令、退出状态或异常}
- {相关 source/file:line 与输入 → 输出关系}
- {生成文件、apply/API 状态或其他必要边界证据}

## 下一步

如果需要，我下一步可以{最小安全检查；涉及修改或重跑时注明“在你明确授权后”}。
```

Keep the two `<br>` tags, use two to five high-signal evidence bullets, and state unreached transitions as not reached rather than healthy. Do not add ownership wording unless asked.

Diagnosis is read-only: do not edit config/templates, rerun deploy or dry-run, apply resources, restart/delete workloads, or recommend code changes without proven source evidence and an explicit fix request. After an authorized fix, claim recovery only when a new observation passes the failed transition.
