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

Every target is a fork under the scfuzzbench org. Its `main` branch holds upstream
code plus the harness, while `pre-target` holds the pristine upstream baseline.
The benchmark consumes the immutable commit recorded in the authoritative
[`benchmarks/targets.json`](benchmarks/targets.json) catalog, with
revision-locked known-bug mappings in
[`benchmarks/known_bugs.json`](benchmarks/known_bugs.json). See
[`benchmarks/README.md`](benchmarks/README.md) for fields and maintenance.

Use the target onboarding skill for new targets:

- `skills/README.md`
- `skills/target-onboarding/SKILL.md`

## Documentation

For all technical/operational details, use the docs site pages:

- Introduction: `docs/introduction.md`
- Start benchmark request: `docs/start.md`
- Methodology: `docs/methodology.md`
- Operations guide (Terraform, running, reruns, analysis, CI workflows): `docs/operations.md`
- Active preliminary results: `docs/preliminary/index.md`
- Target onboarding skill (machine-oriented): `skills/target-onboarding/SKILL.md`

Rendered docs navigation and run/benchmark pages are available under `docs/`.
