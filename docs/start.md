# Start Benchmark

This page creates a **benchmark request** issue in GitHub.

Need a new target first? Use the target onboarding skill at
[`skills/target-onboarding/SKILL.md`](https://github.com/scfuzzbench/scfuzzbench/blob/main/skills/target-onboarding/SKILL.md)
and follow its workflow.

The request moves through GitHub labels:

- `benchmark/01-pending`: added by the issue template on creation.
- `benchmark/02-validated`: added by the bot after JSON validation passes.
- `benchmark/03-approved`: added manually by a maintainer.

Use the preconfigured target selector to fill the repository, immutable commit,
and properties path from the
[target manifest](https://github.com/scfuzzbench/scfuzzbench/blob/main/benchmarks/targets.json).

Every target is a fork under the [scfuzzbench GitHub org](https://github.com/scfuzzbench):
`main` holds upstream code plus the harness, and **`pre-target`** holds the pristine
upstream baseline. Compare `pre-target...main` in the target repo to inspect the
harness; use the manifest's pinned commit for a reproducible run.

<StartBenchmark />

::: warning
Do not put secrets in the issue body. The request is intentionally public/auditable.
:::
