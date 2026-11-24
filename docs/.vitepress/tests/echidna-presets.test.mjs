import assert from "node:assert/strict";
import { test } from "node:test";
import {
  artifactRank, buildRequestFields, pickLinuxArtifact, PREFERRED_ARTIFACT,
  resolveCiBuild, resolvePreset,
} from "../lib/echidna-presets.js";

const PR_COMMIT = "9bc069824e7379277b247a05427ad7ab65bb7a93";
const MASTER_COMMIT = "c6203624d38072f1fa71bf04c73a054423dab1dd";
// GitHub returned the testsuite before the binary for PR #1654's successful CI.
const PR_ARTIFACTS = [
  {
    name: "echidna-testsuite-Linux", expired: false,
    digest: "sha256:b367b12f9b9194b3fde17637d1975a309a6ed5b910bcca4edadb7d2377f752d4",
  },
  {
    name: "echidna-Linux", expired: false,
    digest: "sha256:0bd00d370c7b42630d0446845cb35487c7ce9a3bbb53c8a0033badcf805f739c",
  },
];

function artifact(name, overrides = {}) {
  return { name, expired: false, digest: `sha256:${"a".repeat(64)}`, ...overrides };
}

function fixtureApi(routes) {
  return async (path) => {
    assert.ok(Object.hasOwn(routes, path), `unexpected API request: ${path}`);
    return routes[path];
  };
}

test("PR #1654 selects the fuzzer even when the testsuite is listed first", () => {
  for (const artifacts of [PR_ARTIFACTS, [...PR_ARTIFACTS].reverse()]) {
    assert.equal(pickLinuxArtifact(artifacts), PR_ARTIFACTS[1]);
  }
});

test("redistributable wins regardless of artifact order, with an unexpired legacy fallback", () => {
  const preferred = artifact(PREFERRED_ARTIFACT);
  for (const artifacts of [[...PR_ARTIFACTS, preferred], [preferred, ...PR_ARTIFACTS]]) {
    assert.equal(pickLinuxArtifact(artifacts), preferred);
  }
  assert.equal(
    pickLinuxArtifact([{ ...preferred, expired: true }, ...PR_ARTIFACTS]),
    PR_ARTIFACTS[1],
  );
  assert.equal(pickLinuxArtifact([{ ...preferred, expired: true }]), null);
});

test("testsuites, auxiliary archives and other architectures never become candidates", () => {
  for (const name of [
    "echidna-testsuite-Linux", "echidna-testsuite-linux-amd64",
    "digests-linux-amd64", "echidna-checksums-linux-amd64",
    "echidna-redistributable-aarch64-linux", "echidna-linux-arm64",
    "echidna-Windows", "echidna-macOS", "my-echidna-linux-bundle",
    `${PREFERRED_ARTIFACT}-checksums`,
  ]) {
    assert.equal(artifactRank(artifact(name)), null, name);
    assert.equal(artifactRank(artifact(name), name), null, `preferred ${name}`);
    assert.equal(pickLinuxArtifact([artifact(name)]), null, name);
  }
  assert.equal(artifactRank({ name: "echidna-Linux" }), null);
});

test("a successful run containing only a testsuite is not a usable build", async () => {
  const api = fixtureApi({
    [`/repos/crytic/echidna/actions/runs?head_sha=${PR_COMMIT}&per_page=100`]: {
      workflow_runs: [{ id: 1, status: "completed", conclusion: "success" }],
    },
    "/repos/crytic/echidna/actions/runs/1/artifacts?per_page=100": {
      artifacts: [PR_ARTIFACTS[0]],
    },
  });
  await assert.rejects(resolveCiBuild(api, "crytic/echidna", PR_COMMIT), /no successful run.*supported/);
});

test("master versus PR #1654 pins the actual binary and its digest in the PR variant", async () => {
  const api = fixtureApi({
    "/repos/crytic/echidna/commits/master": { sha: MASTER_COMMIT },
    "/repos/crytic/echidna/pulls/1654": { head: { sha: PR_COMMIT } },
    [`/repos/crytic/echidna/actions/runs?head_sha=${PR_COMMIT}&per_page=100`]: {
      workflow_runs: [
        { id: 45395914927, status: "in_progress", conclusion: null },
        { id: 45395915062, status: "completed", conclusion: "success" },
      ],
    },
    "/repos/crytic/echidna/actions/runs/45395915062/artifacts?per_page=100": {
      artifacts: PR_ARTIFACTS,
    },
    [`/repos/crytic/echidna/actions/runs?head_sha=${MASTER_COMMIT}&per_page=100`]: {
      workflow_runs: [
        { id: 1, status: "completed", conclusion: "success" },
        { id: 2, status: "completed", conclusion: "success" },
      ],
    },
    "/repos/crytic/echidna/actions/runs/1/artifacts?per_page=100": {
      artifacts: [artifact("echidna-Linux")],
    },
    "/repos/crytic/echidna/actions/runs/2/artifacts?per_page=100": {
      artifacts: [artifact(PREFERRED_ARTIFACT)],
    },
  });
  const master = await resolvePreset(api, { kind: "branch", ref: "master" });
  const pull = await resolvePreset(api, { kind: "pull", number: 1654 });
  const request = buildRequestFields([master, pull]);
  assert.equal(request.echidna_ci_artifact_name, PREFERRED_ARTIFACT);
  assert.equal(request.echidna_ci_run_id, "2");
  assert.deepEqual(request.fuzzer_variants, [{
    key: "echidna-pr-1654", base: "echidna",
    ci: {
      run_id: "45395915062", artifact_name: "echidna-Linux",
      artifact_sha256: PR_ARTIFACTS[1].digest.slice("sha256:".length),
      commit: PR_COMMIT,
    },
  }]);
});
