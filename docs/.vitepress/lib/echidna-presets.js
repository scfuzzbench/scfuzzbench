// Resolving Echidna builds for a side-by-side benchmark request.
//
// The start page is static, so the values a request needs -- commit SHAs, CI
// run IDs, artifact digests -- are resolved from the public GitHub API in the
// browser when someone picks the builds to compare. Everything here is pure
// apart from the injected `api` fetcher, so it can be exercised directly.

export const ECHIDNA_REPO = "crytic/echidna";
export const PREFERRED_ARTIFACT = "echidna-redistributable-x86_64-linux";

const VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]*$/;
const COMMIT_RE = /^[A-Fa-f0-9]{40}$/;
const DIGEST_RE = /^[A-Fa-f0-9]{64}$/;

export function githubApi(fetchImpl = fetch) {
  return async function api(path) {
    const response = await fetchImpl(`https://api.github.com${path}`, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (response.status === 403 || response.status === 429) {
      throw new Error(
        "GitHub rate limit reached for this network. Wait a few minutes, or fill the build fields by hand."
      );
    }
    if (!response.ok) {
      throw new Error(`GitHub request failed (${response.status}) for ${path}`);
    }
    return await response.json();
  };
}

/** Drop a single leading "v" from a release tag. */
export function parseReleaseVersion(tag) {
  const version = String(tag || "").trim().replace(/^v/, "");
  if (!VERSION_RE.test(version)) {
    throw new Error(`unusable release tag: ${tag}`);
  }
  return version;
}

/**
 * Rank a candidate artifact, lower being better; null means unusable.
 *
 * CI publishes more than binaries -- checksum bundles like
 * "digests-linux-amd64" also match a bare "linux" test -- and installing one
 * of those would benchmark something that is not the fuzzer at all. So an
 * artifact must name the fuzzer itself to be considered.
 */
export function artifactRank(artifact, preferred = PREFERRED_ARTIFACT, binary = "echidna") {
  if (!artifact || artifact.expired !== false || !artifact.name) {
    return null;
  }
  const name = String(artifact.name);
  if (name === preferred) {
    return 0;
  }
  const namesBinary = new RegExp(`(^|[^a-z])${binary}([^a-z]|$)`, "i").test(name);
  if (!namesBinary || !/linux/i.test(name)) {
    return null;
  }
  return /x86[_-]?64|amd64/i.test(name) ? 1 : 2;
}

/** Pick the artifact that actually carries the fuzzer's Linux binary. */
export function pickLinuxArtifact(artifacts, preferred = PREFERRED_ARTIFACT, binary = "echidna") {
  let best = null;
  let bestRank = null;
  for (const artifact of artifacts || []) {
    const rank = artifactRank(artifact, preferred, binary);
    if (rank === null) {
      continue;
    }
    if (bestRank === null || rank < bestRank) {
      best = artifact;
      bestRank = rank;
    }
  }
  return best;
}

export function artifactDigest(artifact) {
  const digest = String((artifact && artifact.digest) || "").replace(/^sha256:/, "");
  if (!DIGEST_RE.test(digest)) {
    throw new Error(`artifact ${artifact && artifact.name} has no usable digest`);
  }
  return digest.toLowerCase();
}

export async function resolveLatestRelease(api, repo = ECHIDNA_REPO) {
  const release = await api(`/repos/${repo}/releases/latest`);
  const version = parseReleaseVersion(release.tag_name);
  return { kind: "release", version, label: `latest release (${version})` };
}

export async function resolveRefCommit(api, repo, ref) {
  const commit = await api(`/repos/${repo}/commits/${encodeURIComponent(ref)}`);
  const sha = String(commit.sha || "");
  if (!COMMIT_RE.test(sha)) {
    throw new Error(`${repo} ref ${ref} did not resolve to a commit`);
  }
  return sha.toLowerCase();
}

export async function resolvePullRequestHead(api, repo, pullNumber) {
  const number = Number(pullNumber);
  if (!Number.isInteger(number) || number < 1) {
    throw new Error("pull request number must be a positive integer");
  }
  const pull = await api(`/repos/${repo}/pulls/${number}`);
  const sha = String((pull.head && pull.head.sha) || "");
  if (!COMMIT_RE.test(sha)) {
    throw new Error(`could not read the head commit of pull request #${number}`);
  }
  return sha.toLowerCase();
}

/**
 * Find the successful CI run for a commit that published a usable Linux
 * artifact. A build with no such run cannot be benchmarked, so this reports
 * that rather than silently falling back to another revision.
 */
export async function resolveCiBuild(api, repo, commit, preferred = PREFERRED_ARTIFACT) {
  if (!COMMIT_RE.test(String(commit || ""))) {
    throw new Error(`expected a full commit SHA, got ${commit}`);
  }
  const runs = await api(`/repos/${repo}/actions/runs?head_sha=${commit}&per_page=100`);
  const successful = (runs.workflow_runs || []).filter(
    (run) => run.status === "completed" && run.conclusion === "success"
  );
  // Compare candidates across every run rather than taking the first run that
  // happens to publish something usable: several workflows build the same
  // commit, and only one of them publishes the redistributable binary.
  let best = null;
  for (const run of successful) {
    const artifacts = await api(
      `/repos/${repo}/actions/runs/${run.id}/artifacts?per_page=100`
    );
    const artifact = pickLinuxArtifact(artifacts.artifacts, preferred);
    if (!artifact) {
      continue;
    }
    const rank = artifactRank(artifact, preferred);
    if (best === null || rank < best.rank) {
      best = { rank, run, artifact };
    }
    if (rank === 0) {
      break;
    }
  }
  if (best) {
    return {
      kind: "ci",
      run_id: String(best.run.id),
      artifact_name: best.artifact.name,
      artifact_sha256: artifactDigest(best.artifact),
      commit: commit.toLowerCase(),
    };
  }
  throw new Error(
    `no successful run for ${commit.slice(0, 12)} publishes an unexpired Linux Echidna artifact`
  );
}

export async function resolvePreset(api, preset, repo = ECHIDNA_REPO) {
  if (preset.kind === "release") {
    return await resolveLatestRelease(api, repo);
  }
  if (preset.kind === "branch") {
    const commit = await resolveRefCommit(api, repo, preset.ref);
    const build = await resolveCiBuild(api, repo, commit, preset.artifactName);
    return { ...build, label: `${preset.ref} (${commit.slice(0, 12)})` };
  }
  if (preset.kind === "pull") {
    const commit = await resolvePullRequestHead(api, repo, preset.number);
    const build = await resolveCiBuild(api, repo, commit, preset.artifactName);
    return {
      ...build,
      label: `PR #${preset.number} (${commit.slice(0, 12)})`,
      pullNumber: Number(preset.number),
    };
  }
  throw new Error(`unsupported preset: ${preset.kind}`);
}

/** Variant keys must stay prefixed with their base fuzzer. */
export function variantKeyFor(build) {
  if (build.kind === "release") {
    return `echidna-release-${build.version.replace(/[^A-Za-z0-9]+/g, "-")}`.toLowerCase();
  }
  if (build.pullNumber) {
    return `echidna-pr-${build.pullNumber}`;
  }
  return `echidna-${build.commit.slice(0, 12)}`;
}

/**
 * Turn resolved builds into request fields.
 *
 * A variant CI build needs the run-level Echidna CI inputs, so whenever any
 * bleeding-edge build is selected one of them has to be the run-level build
 * and the rest become variants. Release-pinned variants opt out of that build.
 */
export function buildRequestFields(builds, { repo = ECHIDNA_REPO } = {}) {
  // One build is a normal single-fuzzer run; two or more become a comparison.
  const selected = (builds || []).filter(Boolean);
  if (selected.length === 0) {
    throw new Error("pick at least one Echidna build");
  }
  const ciBuilds = selected.filter((build) => build.kind === "ci");
  const commits = ciBuilds.map((build) => build.commit);
  if (new Set(commits).size !== commits.length) {
    throw new Error("two of the selected builds are the same commit");
  }
  const versions = selected
    .filter((build) => build.kind === "release")
    .map((build) => build.version);
  if (new Set(versions).size !== versions.length) {
    throw new Error("two of the selected builds are the same release");
  }

  const runLevel = ciBuilds[0] || selected[0];
  const variants = selected.filter((build) => build !== runLevel);

  const fields = {
    echidna_version: "",
    echidna_ci_repo: "",
    echidna_ci_run_id: "",
    echidna_ci_artifact_name: "",
    echidna_ci_artifact_sha256: "",
    echidna_ci_commit: "",
    fuzzers: ["echidna"],
    fuzzer_variants: [],
  };
  if (runLevel.kind === "ci") {
    fields.echidna_ci_repo = `https://github.com/${repo}`;
    fields.echidna_ci_run_id = runLevel.run_id;
    fields.echidna_ci_artifact_name = runLevel.artifact_name;
    fields.echidna_ci_artifact_sha256 = runLevel.artifact_sha256;
    fields.echidna_ci_commit = runLevel.commit;
  } else {
    fields.echidna_version = runLevel.version;
  }

  for (const build of variants) {
    const key = variantKeyFor(build);
    fields.fuzzers.push(key);
    fields.fuzzer_variants.push(
      build.kind === "ci"
        ? {
            key,
            base: "echidna",
            ci: {
              run_id: build.run_id,
              artifact_name: build.artifact_name,
              artifact_sha256: build.artifact_sha256,
              commit: build.commit,
            },
          }
        : { key, base: "echidna", version: build.version }
    );
  }
  return fields;
}
