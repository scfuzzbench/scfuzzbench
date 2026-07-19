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
  "target_repo_url": "https://github.com/scfuzzbench/aave-v4-scfuzzbench",
  "target_commit": "main",
  "benchmark_type": "property",
  "instance_type": "c6a.4xlarge",
  "instances_per_fuzzer": 4,
  "timeout_hours": 1,
  "fuzzers": ["echidna", "medusa", "foundry", "recon-fuzzer"],
  "foundry_version": "",
  "foundry_git_repo": "",
  "foundry_git_ref": "",
  "echidna_version": "",
  "echidna_ci_repo": "",
  "echidna_ci_run_id": "",
  "echidna_ci_artifact_name": "",
  "echidna_ci_artifact_sha256": "",
  "echidna_ci_commit": "",
  "echidna_ci_token_ssm_parameter_name": "",
  "echidna_ci_token_kms_key_arn": "",
  "medusa_version": "",
  "medusa_git_repo": "",
  "medusa_git_ref": "",
  "medusa_git_commit": "",
  "medusa_go_version": "1.24.0",
  "medusa_go_sha256": "dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858",
  "recon_version": "",
  "git_token_ssm_parameter_name": "/scfuzzbench/recon/github_token",
  "properties_path": "",
  "fuzzer_env_json": ""
}
```
