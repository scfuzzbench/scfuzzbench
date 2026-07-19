# Benchmark target manifest

[`targets.json`](targets.json) is the authoritative catalog of active benchmark
targets. The docs site and the preconfigured benchmark request form read it
directly, so target metadata must not be copied into those consumers.

The catalog records selection metadata only. Ground-truth bug identifiers and
aliases live in the separate, revision-locked
[`known_bugs.json`](known_bugs.json) catalog.

## Fields

Each target contains:

- `id`: stable, kebab-case identifier used by the UI.
- `label`: human-readable target name.
- `repo`: canonical HTTPS repository URL.
- `commit`: immutable, lowercase 40-character Git commit SHA.
- `properties_path`: repository-relative Solidity properties file used by
  `SCFUZZBENCH_PROPERTIES_PATH`.
- `rationale`: why the target improves the suite's representativeness.
- `related_work_refs`: prior benchmark, fuzzing campaign, or property-suite
  references, each with a URL and description.
- `overlap_group`: identifier shared by targets with substantially overlapping
  application code.
- `overlap_notes`: explicit assessment of overlap with the rest of the active
  suite.

## Add or update a target

1. Apply the selection policy in
   [`docs/methodology.md`](../docs/methodology.md#choosing-target-projects).
2. Complete the target onboarding workflow and merge its harness into the
   target repository's `main` branch.
3. Resolve that branch to a full commit SHA. Verify that the properties file
   exists at the same commit.
4. Add or update one manifest entry. Record prior fuzzing/property work when it
   exists, and document any application-code overlap.
5. Run:

   ```bash
   make targets-validate
   npm run docs:build
   ```

6. In the pull request, explain the selection or pin change and link the target
   harness review.

Never use a branch or tag in `commit`. A target update is a reviewed manifest
change, even when the target repository's `main` branch moved only to adjust
its harness.

## Remove a target

Remove an entry only through review and explain why it no longer belongs in the
active suite. Historical run manifests and published artifacts remain
unchanged; removing a target from this catalog only removes it from the current
curated list and request preset.

## Ground-truth known bugs

[`known_bugs.json`](known_bugs.json) maps evidence-backed known bugs and
health-check canaries to the normalized event names emitted by each fuzzer.
Every entry is tied to the same target ID and immutable commit as
[`targets.json`](targets.json). The validator rejects missing targets, revision
drift, ambiguous fuzzer/event aliases, and entries without commit-pinned
evidence.

Canaries are deliberately failing harness checks. They verify that assertion
and invariant failure paths work, but they are reported separately and never
count toward known-bug hit rates.

Known-bug entries must be conservative:

- Link evidence at the pinned target revision.
- Give one canonical ID to aliases that demonstrate the same reviewed bug.
- Do not promote an event to a known bug only because it crashes or falsifies a
  property.
- Leave findings unmapped when root-cause evidence is incomplete.

When a target commit changes, re-review every ground-truth entry and update
`target_commit` and its pinned evidence together. Run `make targets-validate`
after any target or ground-truth change.
