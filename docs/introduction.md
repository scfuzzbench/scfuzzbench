# Introduction

Welcome to `scfuzzbench`, an up-to-date, practical benchmark for smart-contract fuzzers focused in stateful invariant testing.

## Motivation

- Maintain a current view of common fuzzers under a shared, realistic workload.
- Focus on benchmark quality:
  - real projects
  - real bug-finding tasks
  - long timeouts
  - repeated runs to reduce noise and compare medians/distributions
  - transparent metrics and artifacts for independent review
- Help fuzzer/tool builders understand bottlenecks and improve their tools.
- Leave room for iterative improvements in setup and fairness (for example, corpus bootstrapping strategies).

## Inclusion criteria for fuzzers

A fuzzer is considered in-scope when it is:

- Open source.
- Able to run assertion failures.
- Able to run global invariants.

## Fuzzers currently ready for this benchmark

- Foundry
- Echidna
- Medusa

## Benchmark targets

- [Aave v4](https://github.com/Recon-Fuzz/aave-v4-scfuzzbench)
- [Superform v2-periphery](https://github.com/Recon-Fuzz/superform-v2-periphery-scfuzzbench)
- [Liquity v2 Governance](https://github.com/Recon-Fuzz/liquity-V2-gov-scfuzzbench)
- [Nerite](https://github.com/Recon-Fuzz/nerite-scfuzzbench)

## Notable fuzzers currently excluded

These are notable tools, but currently excluded from this benchmark because they do not meet one or more criteria above:

- [Orca](https://docs.veridise.com/orca/): not open source.
- [ItyFuzz](https://docs.ityfuzz.rs/): not straightforward for assertion-failure/property style runs in this workflow.
- [Wake](https://github.com/Ackee-Blockchain/wake): Python-based workflow that requires a custom harness.
- [Harvey](https://dl.acm.org/doi/10.1145/3368089.3417064): closed source.

As tools evolve, this list should be revisited.
