# Start Benchmark

This page creates a **benchmark request** issue in GitHub.

Need a new target first? Use the target onboarding skill at
[`skills/target-onboarding/SKILL.md`](https://github.com/scfuzzbench/scfuzzbench/blob/main/skills/target-onboarding/SKILL.md)
and follow its workflow.

The request moves through GitHub labels:

- `benchmark/01-pending`: added by the issue template on creation.
- `benchmark/02-validated`: added by the bot after JSON validation passes.
- `benchmark/03-approved`: added manually by a maintainer.

Use the preconfigured target selector to auto-fill target repo/commit for the current benchmark targets listed in `README.md`.

Every target is a fork under the [scfuzzbench GitHub org](https://github.com/scfuzzbench):
the benchmark always consumes the **`main`** branch (upstream code plus the harness), and
**`pre-target`** holds the pristine upstream baseline — compare `pre-target...main` in the
target repo to see exactly what the harness adds.

Current preconfigured targets (all at `main`):

- [Aave v4](https://github.com/scfuzzbench/aave-v4-scfuzzbench)
- [Superform v2-periphery](https://github.com/scfuzzbench/superform-v2-periphery-scfuzzbench)
- [Liquity v2 Governance](https://github.com/scfuzzbench/liquity-V2-gov-scfuzzbench)
- [Origin Dollar (OUSD)](https://github.com/scfuzzbench/origin-dollar-scfuzzbench)
- [Drips](https://github.com/scfuzzbench/drips-fuzzing-scfuzzbench)

<StartBenchmark />

::: warning
Do not put secrets in the issue body. The request is intentionally public/auditable.
:::
