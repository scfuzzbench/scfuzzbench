---
name: Benchmark request
about: Request a new scfuzzbench benchmark run (3-step workflow).
title: "benchmark: <org>/<repo>@<ref>"
labels: "benchmark/01-pending"
---

<!-- scfuzzbench-benchmark-request:v1 -->

Paste a JSON request below.

Notes:
- Do not include secrets in this issue.
- Step `01` (`benchmark/01-pending`) is applied by this template at issue creation.
- Step `02` (`benchmark/02-validated`) is applied by the bot after JSON validation passes.
- Step `03` (`benchmark/03-approved`) is applied manually by a maintainer to start the run.
- Limits: `instances_per_fuzzer` must be in `[1, 20]`, `timeout_hours` must be in `[0.25, 72]`.
- Preliminary checkpoints default to 60 minutes. Set `preliminary_interval_minutes` to `0` to disable them.

```json
{
  "target_repo_url": "https://github.com/scfuzzbench/aave-v4-scfuzzbench",
  "target_commit": "main",
  "benchmark_type": "property",
  "instance_type": "c6a.4xlarge",
  "instances_per_fuzzer": 4,
  "timeout_hours": 1,
  "preliminary_interval_minutes": 60,
  "fuzzers": ["echidna", "medusa", "foundry", "recon-fuzzer"],
  "foundry_version": "",
  "foundry_git_repo": "",
  "foundry_git_ref": "",
  "echidna_version": "",
  "medusa_version": "",
  "recon_version": "",
  "git_token_ssm_parameter_name": "/scfuzzbench/recon/github_token",
  "properties_path": "",
  "fuzzer_env_json": ""
}
```
