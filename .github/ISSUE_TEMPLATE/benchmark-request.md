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

```json
{
  "target_repo_url": "https://github.com/Recon-Fuzz/aave-v4-scfuzzbench",
  "target_commit": "v0.5.6-recon",
  "benchmark_type": "property",
  "instance_type": "c6a.4xlarge",
  "instances_per_fuzzer": 4,
  "timeout_hours": 1,
  "fuzzers": ["echidna", "medusa", "foundry"],
  "foundry_version": "",
  "foundry_git_repo": "https://github.com/0xalpharush/foundry",
  "foundry_git_ref": "fail_on_assert",
  "echidna_version": "",
  "medusa_version": "",
  "bitwuzla_version": "",
  "git_token_ssm_parameter_name": "/scfuzzbench/recon/github_token",
  "properties_path": "",
  "fuzzer_env_json": ""
}
```
