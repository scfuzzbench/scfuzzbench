# scfuzzbench

Benchmark suite for smart-contract fuzzers.

🚀 **Support us on TheDAO Security Fund! https://giveth.io/project/scfuzzbench:-smart-contract-fuzzer-benchmark-suite**


<table>
  <tr>
    <td><img src="docs/public/images/sample-run/bugs_over_time.png" alt="Bugs over time" width="420"></td>
    <td><img src="docs/public/images/sample-run/time_to_k.png" alt="Time to k" width="420"></td>
  </tr>
  <tr>
    <td><img src="docs/public/images/sample-run/final_distribution.png" alt="Final distribution" width="420"></td>
    <td><img src="docs/public/images/sample-run/plateau_and_late_share.png" alt="Plateau and late share" width="420"></td>
  </tr>
</table>

## Motivation

- Maintain a current view of common fuzzers under a shared, realistic workload.
- Focus on benchmark quality with real projects, real bug-finding tasks, long timeouts, and repeated runs.
- Publish transparent metrics and artifacts for independent review.
- Help fuzzer/tool builders identify bottlenecks and improve their tools.

## Inclusion Criteria For Fuzzers

A fuzzer is currently considered in-scope when it is:

- Open source.
- Able to run assertion failures.
- Able to run global invariants.

## Fuzzers Currently Ready

- Foundry
- Echidna
- Medusa
- Recon Fuzzer

## Benchmark Targets

Every target is a fork under the scfuzzbench org. The benchmark always consumes the
target's `main` branch (upstream code plus harness); `pre-target` holds the pristine
upstream baseline, so `compare pre-target...main` shows exactly what the harness adds.

- [Aave v4](https://github.com/scfuzzbench/aave-v4-scfuzzbench)
- [Superform v2-periphery](https://github.com/scfuzzbench/superform-v2-periphery-scfuzzbench)
- [Liquity v2 Governance](https://github.com/scfuzzbench/liquity-V2-gov-scfuzzbench)
- [Origin Dollar (OUSD)](https://github.com/scfuzzbench/origin-dollar-scfuzzbench)
- [Drips](https://github.com/scfuzzbench/drips-fuzzing-scfuzzbench)

Use the target onboarding skill for new targets:

- `skills/README.md`
- `skills/target-onboarding/SKILL.md`

## Documentation

For all technical/operational details, use the docs site pages:

- Introduction: `docs/introduction.md`
- Start benchmark request: `docs/start.md`
- Methodology: `docs/methodology.md`
- Operations guide (Terraform, running, reruns, analysis, CI workflows): `docs/operations.md`
- Target onboarding skill (machine-oriented): `skills/target-onboarding/SKILL.md`

Rendered docs navigation and run/benchmark pages are available under `docs/`.
