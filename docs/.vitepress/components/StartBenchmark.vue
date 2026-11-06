<script setup lang="ts">
import { computed, ref, watch } from "vue";
import targetManifest from "../../../benchmarks/targets.json";
import ec2Pricing from "../generated/ec2-pricing.json";

type BenchmarkType = "property" | "optimization";
type Ec2PricingTable = Record<string, number>;
type PreconfiguredTarget = {
  id: string;
  label: string;
  repoUrl: string;
  commit: string;
  propertiesPath: string;
};

const REPO_OWNER = "scfuzzbench";
const REPO_NAME = "scfuzzbench";
const NEW_ISSUE_URL = `https://github.com/${REPO_OWNER}/${REPO_NAME}/issues/new`;
const PRECONFIGURED_TARGETS: PreconfiguredTarget[] = targetManifest.targets.map((target) => ({
  id: target.id,
  label: target.label,
  repoUrl: target.repo,
  commit: target.commit,
  propertiesPath: target.properties_path,
}));
const CUSTOM_TARGET_ID = "custom";

// Defaults are intentionally aligned with the repo's typical local `.env` values.
// Avoid putting anything secret here: this is a fully static site.
const targetRepoUrl = ref(PRECONFIGURED_TARGETS[0].repoUrl);
const targetCommit = ref(PRECONFIGURED_TARGETS[0].commit);
const propertiesPath = ref(PRECONFIGURED_TARGETS[0].propertiesPath);
const selectedPreconfiguredTargetId = ref(PRECONFIGURED_TARGETS[0].id);
const isApplyingPreconfiguredTarget = ref(false);

const benchmarkType = ref<BenchmarkType>("property");
const instanceType = ref("c6a.4xlarge");
const instancesPerFuzzer = ref(4);
const timeoutHours = ref(1);
const preliminaryIntervalMinutes = ref(60);

// Dynamically discover fuzzers from committed run scripts.
// In CI tarball checkouts, this naturally reflects the tracked repo content.
const discoveredFuzzerRuns = import.meta.glob("../../../fuzzers/*/run.sh", {
  query: "?raw",
  import: "default",
});

function orderFuzzers(keys: string[]): string[] {
  const unique = Array.from(new Set(keys));
  const preferred = ["echidna", "medusa", "foundry", "recon-fuzzer"];
  const orderedPreferred = preferred.filter((name) => unique.includes(name));
  const extras = unique.filter((name) => !preferred.includes(name)).sort();
  return [...orderedPreferred, ...extras];
}

function extractFuzzerName(path: string): string | null {
  const match = path.match(/\/fuzzers\/([^/]+)\/run\.sh$/);
  return match ? match[1] : null;
}

const allFuzzerKeys = orderFuzzers(
  Object.keys(discoveredFuzzerRuns)
    .map(extractFuzzerName)
    .filter((name): name is string => Boolean(name))
);

// Echidna is selected through its build options below, not this list.
const selectedFuzzerKeys = ref<string[]>(
  allFuzzerKeys.filter((name) => name !== "echidna")
);
const participatingFuzzerKeys = computed(() => {
  const selected = new Set(selectedFuzzerKeys.value);
  return allFuzzerKeys.filter((name) => selected.has(name));
});

// Advanced / optional overrides.
const foundryGitRepo = ref("");
const foundryGitRef = ref("");

const echidnaVersion = ref("");
const echidnaCiRepo = ref("");
const echidnaCiRunId = ref("");
const echidnaCiArtifactName = ref("");
const echidnaCiArtifactSha256 = ref("");
const echidnaCiCommit = ref("");
const echidnaCiTokenSsmParameterName = ref("");
const echidnaCiTokenKmsKeyArn = ref("");
const medusaVersion = ref("");
const medusaGitRepo = ref("");
const medusaGitRef = ref("");
const medusaGitCommit = ref("");
const medusaGoVersion = ref("1.24.0");
const medusaGoSha256 = ref("dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858");
const reconVersion = ref("");

const gitTokenSsmParameterName = ref("/scfuzzbench/recon/github_token");

const fuzzerEnvJson = ref("");
const fuzzerVariantsJson = ref("");

// Echidna appears in the fuzzer list once per build people compare: the
// published release, master, and a pull request. The page is static, so the
// commit SHAs, CI run IDs and artifact digests those need are resolved from
// the public GitHub API as soon as a build is picked.
type ResolvedBuild = {
  kind: string;
  label: string;
  version?: string;
  run_id?: string;
  artifact_name?: string;
  artifact_sha256?: string;
  commit?: string;
  pullNumber?: number;
};

const echidnaOptions = [
  { id: "release", label: "latest" },
  { id: "master", label: "master" },
  { id: "pull", label: "PR" },
] as const;

const otherFuzzerKeys = allFuzzerKeys.filter((name) => name !== "echidna");
const selectedEchidnaOptions = ref<string[]>(["release"]);
const echidnaPullNumber = ref("");
const echidnaBuildStatus = ref<Record<string, string>>({});
const echidnaResolveError = ref("");
const resolvedEchidnaBuilds = ref<ResolvedBuild[]>([]);

// A CI build installs from a token-protected Actions artifact, so a request
// using one is incomplete without the parameter holding that token.
const echidnaNeedsToken = computed(
  () =>
    resolvedEchidnaBuilds.value.some((build) => build.kind === "ci") &&
    !echidnaCiTokenSsmParameterName.value.trim()
);

function echidnaPresetsFor(selection: string[]): Record<string, unknown>[] {
  const presets: Record<string, unknown>[] = [];
  if (selection.includes("release")) presets.push({ kind: "release", id: "release" });
  if (selection.includes("master")) {
    presets.push({ kind: "branch", ref: "master", id: "master" });
  }
  if (selection.includes("pull") && /^[0-9]+$/.test(echidnaPullNumber.value.trim())) {
    presets.push({
      kind: "pull",
      number: Number(echidnaPullNumber.value.trim()),
      id: "pull",
    });
  }
  return presets;
}

function clearEchidnaBuildFields() {
  echidnaVersion.value = "";
  echidnaCiRepo.value = "";
  echidnaCiRunId.value = "";
  echidnaCiArtifactName.value = "";
  echidnaCiArtifactSha256.value = "";
  echidnaCiCommit.value = "";
  fuzzerVariantsJson.value = "";
  resolvedEchidnaBuilds.value = [];
}

let echidnaResolveToken = 0;

async function resolveEchidnaSelection() {
  const selection = [...selectedEchidnaOptions.value];
  const presets = echidnaPresetsFor(selection);
  echidnaResolveError.value = "";

  if (presets.length === 0) {
    echidnaBuildStatus.value = {};
    clearEchidnaBuildFields();
    return;
  }
  // The published release needs no lookup, so avoid spending a request on it.
  if (presets.length === 1 && presets[0].kind === "release") {
    echidnaBuildStatus.value = { release: "" };
    clearEchidnaBuildFields();
    return;
  }

  const token = ++echidnaResolveToken;
  const pending: Record<string, string> = {};
  for (const preset of presets) {
    pending[String(preset.id)] = "resolving…";
  }
  echidnaBuildStatus.value = pending;

  try {
    const { githubApi, resolvePreset, buildRequestFields } = await import(
      "../lib/echidna-presets.js"
    );
    const api = githubApi();
    const builds: ResolvedBuild[] = [];
    const status: Record<string, string> = {};
    for (const preset of presets) {
      const build = (await resolvePreset(api, preset)) as ResolvedBuild;
      builds.push(build);
      status[String(preset.id)] =
        build.kind === "ci"
          ? `${build.commit!.slice(0, 12)} · ${build.artifact_sha256!.slice(0, 12)}…`
          : build.version!;
    }
    // A newer selection started while this one was in flight.
    if (token !== echidnaResolveToken) {
      return;
    }

    const fields = buildRequestFields(builds);
    echidnaVersion.value = fields.echidna_version;
    echidnaCiRepo.value = fields.echidna_ci_repo;
    echidnaCiRunId.value = fields.echidna_ci_run_id;
    echidnaCiArtifactName.value = fields.echidna_ci_artifact_name;
    echidnaCiArtifactSha256.value = fields.echidna_ci_artifact_sha256;
    echidnaCiCommit.value = fields.echidna_ci_commit;
    fuzzerVariantsJson.value = fields.fuzzer_variants.length
      ? JSON.stringify(fields.fuzzer_variants)
      : "";
    resolvedEchidnaBuilds.value = builds;
    echidnaBuildStatus.value = status;
  } catch (error) {
    if (token !== echidnaResolveToken) {
      return;
    }
    clearEchidnaBuildFields();
    echidnaBuildStatus.value = {};
    echidnaResolveError.value =
      error instanceof Error ? error.message : String(error);
  }
}

let echidnaResolveTimer: ReturnType<typeof setTimeout> | undefined;

watch([selectedEchidnaOptions, echidnaPullNumber], () => {
  // Typing a pull request number should not fire a request per keystroke.
  clearTimeout(echidnaResolveTimer);
  echidnaResolveTimer = setTimeout(resolveEchidnaSelection, 500);
});

function normalizeRepoUrl(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/\.git\/?$/, "")
    .replace(/\/+$/, "");
}

function normalizeCommitRef(raw: string): string {
  return raw.trim();
}

function shortCommit(raw: string): string {
  return raw.slice(0, 12);
}

function findPreconfiguredTarget(repoUrl: string, commit: string): PreconfiguredTarget | null {
  const normalizedRepo = normalizeRepoUrl(repoUrl);
  const normalizedCommit = normalizeCommitRef(commit);
  return (
    PRECONFIGURED_TARGETS.find(
      (target) =>
        normalizeRepoUrl(target.repoUrl) === normalizedRepo &&
        normalizeCommitRef(target.commit) === normalizedCommit
    ) ?? null
  );
}

watch(selectedPreconfiguredTargetId, (selectedId) => {
  if (selectedId === CUSTOM_TARGET_ID) {
    return;
  }
  const selected = PRECONFIGURED_TARGETS.find((target) => target.id === selectedId);
  if (!selected) {
    return;
  }
  isApplyingPreconfiguredTarget.value = true;
  targetRepoUrl.value = selected.repoUrl;
  targetCommit.value = selected.commit;
  propertiesPath.value = selected.propertiesPath;
  isApplyingPreconfiguredTarget.value = false;
});

watch([targetRepoUrl, targetCommit], ([nextRepo, nextCommit]) => {
  if (isApplyingPreconfiguredTarget.value) {
    return;
  }
  const matched = findPreconfiguredTarget(nextRepo, nextCommit);
  selectedPreconfiguredTargetId.value = matched ? matched.id : CUSTOM_TARGET_ID;
});

const pricesUsdPerHour = computed<Ec2PricingTable>(() => {
  const raw = (ec2Pricing as { prices_usd_per_hour?: Ec2PricingTable }).prices_usd_per_hour;
  return raw && typeof raw === "object" ? raw : {};
});

const estimatedCostUsd = computed<number | null>(() => {
  const perInstanceHour = pricesUsdPerHour.value[instanceType.value.trim()];
  if (!Number.isFinite(perInstanceHour) || perInstanceHour <= 0) {
    return null;
  }
  const selectedFuzzers = requestedFuzzerKeys.value.length;
  const instances = Number(instancesPerFuzzer.value);
  const hours = Number(timeoutHours.value);
  if (!Number.isFinite(instances) || instances <= 0 || !Number.isFinite(hours) || hours <= 0 || selectedFuzzers <= 0) {
    return null;
  }
  return perInstanceHour * instances * selectedFuzzers * hours;
});

const estimatedCostLabel = computed(() => {
  const value = estimatedCostUsd.value;
  if (value === null) {
    return "";
  }
  let formatted: string;
  if (value < 100) {
    formatted = value.toFixed(2);
  } else if (value < 1000) {
    formatted = value.toFixed(1);
  } else {
    formatted = Math.round(value).toString();
  }
  return `~$ ${formatted}`;
});

const normalizedFuzzerEnvJson = computed(() => {
  const raw = fuzzerEnvJson.value.trim();
  if (!raw) {
    return "";
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return raw;
    }
    return JSON.stringify(parsed);
  } catch {
    return raw;
  }
});

// Variants run one of the selected fuzzers a second time at another revision,
// e.g. two Echidna releases in the same benchmark.
const parsedFuzzerVariants = computed<Record<string, unknown>[]>(() => {
  const raw = fuzzerVariantsJson.value.trim();
  if (!raw) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(
      (entry): entry is Record<string, unknown> =>
        Boolean(entry) && typeof entry === "object" && !Array.isArray(entry)
    );
  } catch {
    return [];
  }
});

const fuzzerVariantKeys = computed(() =>
  parsedFuzzerVariants.value
    .map((variant) => (typeof variant.key === "string" ? variant.key.trim() : ""))
    .filter((key) => key.length > 0)
);

const requestedFuzzerKeys = computed(() => {
  const keys = [...participatingFuzzerKeys.value, ...fuzzerVariantKeys.value];
  if (selectedEchidnaOptions.value.length > 0) {
    keys.unshift("echidna");
  }
  return Array.from(new Set(keys));
});

const requestJson = computed(() => {
  const payload: Record<string, unknown> = {
    target_repo_url: targetRepoUrl.value.trim(),
    target_commit: targetCommit.value.trim(),
    benchmark_type: benchmarkType.value,
    instance_type: instanceType.value.trim(),
    instances_per_fuzzer: instancesPerFuzzer.value,
    timeout_hours: timeoutHours.value,
    preliminary_interval_minutes: preliminaryIntervalMinutes.value,
    fuzzers: requestedFuzzerKeys.value,
    fuzzer_variants: parsedFuzzerVariants.value,

    foundry_git_repo: foundryGitRepo.value.trim(),
    foundry_git_ref: foundryGitRef.value.trim(),

    echidna_version: echidnaVersion.value.trim(),
    echidna_ci_repo: echidnaCiRepo.value.trim(),
    echidna_ci_run_id: echidnaCiRunId.value.trim(),
    echidna_ci_artifact_name: echidnaCiArtifactName.value.trim(),
    echidna_ci_artifact_sha256: echidnaCiArtifactSha256.value.trim(),
    echidna_ci_commit: echidnaCiCommit.value.trim(),
    echidna_ci_token_ssm_parameter_name: echidnaCiTokenSsmParameterName.value.trim(),
    echidna_ci_token_kms_key_arn: echidnaCiTokenKmsKeyArn.value.trim(),
    medusa_version: medusaVersion.value.trim(),
    medusa_git_repo: medusaGitRepo.value.trim(),
    medusa_git_ref: medusaGitRef.value.trim(),
    medusa_git_commit: medusaGitCommit.value.trim(),
    medusa_go_version: medusaGoVersion.value.trim(),
    medusa_go_sha256: medusaGoSha256.value.trim(),
    recon_version: reconVersion.value.trim(),

    git_token_ssm_parameter_name: gitTokenSsmParameterName.value.trim(),

    properties_path: propertiesPath.value.trim(),
    fuzzer_env_json: normalizedFuzzerEnvJson.value,
  };

  return JSON.stringify(payload, null, 2);
});

const issueTitle = computed(() => {
  const repo = targetRepoUrl.value.trim().replace(/^https?:\/\//, "");
  const refPart = targetCommit.value.trim() ? `@${targetCommit.value.trim()}` : "";
  const typePart = benchmarkType.value ? ` (${benchmarkType.value})` : "";
  return `benchmark: ${repo}${refPart}${typePart}`;
});

const issueBody = computed(() => {
  return [
    "<!-- scfuzzbench-benchmark-request:v1 -->",
    "",
    "This issue was generated from https://scfuzzbench.com/start.",
    "",
    "A maintainer must apply `benchmark/03-approved` to start the run.",
    "",
    "```json",
    requestJson.value,
    "```",
    "",
    "Notes:",
    "- Do not include secrets in this issue.",
    "- Preconfigured targets use an immutable commit from the in-repo target manifest.",
    "- Custom `target_commit` values may be a commit SHA, tag, or branch name.",
  ].join("\n");
});

const issueUrl = computed(() => {
  const params = new URLSearchParams();
  params.set("title", issueTitle.value);
  params.set("body", issueBody.value);
  // These labels must exist in the repo to be pre-applied; the workflow will also ensure them.
  params.set("labels", "benchmark/01-pending");
  return `${NEW_ISSUE_URL}?${params.toString()}`;
});

const showAdvanced = ref(false);
</script>

<template>
  <div class="sb-start">
    <div class="sb-start__panel">
      <div class="sb-start__grid">
        <label class="sb-start__field">
          <div class="sb-start__label">Preconfigured target</div>
          <select v-model="selectedPreconfiguredTargetId" class="sb-start__input">
            <option
              v-for="target in PRECONFIGURED_TARGETS"
              :key="target.id"
              :value="target.id"
            >
              {{ target.label }} ({{ shortCommit(target.commit) }})
            </option>
            <option :value="CUSTOM_TARGET_ID">Custom</option>
          </select>
        </label>

        <label class="sb-start__field">
          <div class="sb-start__label">Target repo URL</div>
          <input v-model="targetRepoUrl" class="sb-start__input" type="text" />
        </label>

        <label class="sb-start__field">
          <div class="sb-start__label">Target commit / tag / branch</div>
          <input v-model="targetCommit" class="sb-start__input" type="text" />
        </label>

        <label class="sb-start__field">
          <div class="sb-start__label">Benchmark type</div>
          <select v-model="benchmarkType" class="sb-start__input">
            <option value="property">property</option>
            <option value="optimization">optimization</option>
          </select>
        </label>

        <label class="sb-start__field">
          <div class="sb-start__label">EC2 instance type</div>
          <input v-model="instanceType" class="sb-start__input" type="text" />
        </label>

        <label class="sb-start__field">
          <div class="sb-start__label">Instances per fuzzer (1 to 20)</div>
          <input
            v-model.number="instancesPerFuzzer"
            class="sb-start__input"
            type="number"
            min="1"
            max="20"
            step="1"
          />
        </label>

        <label class="sb-start__field">
          <div class="sb-start__label">Timeout (hours, 0.25 to 72)</div>
          <input
            v-model.number="timeoutHours"
            class="sb-start__input"
            type="number"
            min="0.25"
            max="72"
            step="0.25"
          />
        </label>

        <label class="sb-start__field">
          <div class="sb-start__label">Preliminary updates (minutes; 0 turns them off)</div>
          <input
            v-model.number="preliminaryIntervalMinutes"
            class="sb-start__input"
            type="number"
            min="0"
            max="1440"
            step="1"
          />
        </label>

        <label class="sb-start__field sb-start__field--full">
          <div class="sb-start__label">Fuzzers</div>
          <div class="sb-start__fuzzers">
            <label
              v-for="option in echidnaOptions"
              :key="option.id"
              class="sb-start__fuzzer-option"
            >
              <input
                v-model="selectedEchidnaOptions"
                class="sb-start__fuzzer-checkbox"
                type="checkbox"
                :value="option.id"
              />
              <span>
                <code>echidna</code> ({{ option.label }})
                <input
                  v-if="option.id === 'pull'"
                  v-model="echidnaPullNumber"
                  class="sb-start__input sb-start__input--inline"
                  type="text"
                  inputmode="numeric"
                  placeholder="1614"
                  :disabled="!selectedEchidnaOptions.includes('pull')"
                />
                <small v-if="echidnaBuildStatus[option.id]" class="sb-start__build-note">
                  {{ echidnaBuildStatus[option.id] }}
                </small>
              </span>
            </label>

            <label
              v-for="fuzzer in otherFuzzerKeys"
              :key="fuzzer"
              class="sb-start__fuzzer-option"
            >
              <input
                v-model="selectedFuzzerKeys"
                class="sb-start__fuzzer-checkbox"
                type="checkbox"
                :value="fuzzer"
              />
              <span><code>{{ fuzzer }}</code></span>
            </label>
          </div>

          <p v-if="echidnaResolveError" class="sb-start__hint sb-start__hint--error">
            {{ echidnaResolveError }}
          </p>
          <p v-if="echidnaNeedsToken" class="sb-start__hint sb-start__hint--error">
            A <code>master</code> or pull request build also needs
            <code>echidna_ci_token_ssm_parameter_name</code> under advanced settings.
          </p>
        </label>
      </div>

      <div class="sb-start__actions">
        <a class="sb-start__button" :href="issueUrl" target="_blank" rel="noreferrer">
          Open GitHub request issue
        </a>

        <button class="sb-start__button sb-start__button--ghost" type="button" @click="showAdvanced = !showAdvanced">
          {{ showAdvanced ? "Hide advanced" : "Show advanced" }}
        </button>
        <div v-if="estimatedCostLabel" class="sb-start__cost">{{ estimatedCostLabel }}</div>
      </div>

      <div v-if="showAdvanced" class="sb-start__advanced">
        <div class="sb-start__grid">
          <label class="sb-start__field">
            <div class="sb-start__label">GitHub token SSM parameter name (for private repos)</div>
            <input v-model="gitTokenSsmParameterName" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Foundry git repo (build from source, optional)</div>
            <input v-model="foundryGitRepo" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Foundry git ref (optional)</div>
            <input v-model="foundryGitRef" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna version override (optional)</div>
            <input v-model="echidnaVersion" class="sb-start__input" type="text" placeholder="e.g. 2.3.1" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna CI repository (artifact mode)</div>
            <input v-model="echidnaCiRepo" class="sb-start__input" type="text" placeholder="https://github.com/crytic/echidna" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna CI run ID</div>
            <input v-model="echidnaCiRunId" class="sb-start__input" type="text" inputmode="numeric" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna Linux artifact name</div>
            <input v-model="echidnaCiArtifactName" class="sb-start__input" type="text" placeholder="echidna-Linux" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna artifact SHA-256</div>
            <input v-model="echidnaCiArtifactSha256" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna CI full commit SHA</div>
            <input v-model="echidnaCiCommit" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna artifact token SSM parameter</div>
            <input
              v-model="echidnaCiTokenSsmParameterName"
              class="sb-start__input"
              type="text"
              placeholder="/scfuzzbench/echidna/actions_token"
            />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Echidna token KMS key ARN (optional)</div>
            <input
              v-model="echidnaCiTokenKmsKeyArn"
              class="sb-start__input"
              type="text"
              placeholder="Blank for the aws/ssm managed key"
            />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Medusa version override (optional)</div>
            <input v-model="medusaVersion" class="sb-start__input" type="text" placeholder="e.g. 1.4.1" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Medusa git repository (source mode)</div>
            <input v-model="medusaGitRepo" class="sb-start__input" type="text" placeholder="https://github.com/crytic/medusa" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Medusa git ref</div>
            <input v-model="medusaGitRef" class="sb-start__input" type="text" placeholder="master" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Medusa full commit SHA</div>
            <input v-model="medusaGitCommit" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Medusa source Go version</div>
            <input v-model="medusaGoVersion" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Medusa Go archive SHA-256</div>
            <input v-model="medusaGoSha256" class="sb-start__input" type="text" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Recon Fuzzer version override (optional)</div>
            <input v-model="reconVersion" class="sb-start__input" type="text" placeholder="e.g. 0.4.6" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Properties path (optional)</div>
            <input v-model="propertiesPath" class="sb-start__input" type="text" placeholder="repo-relative path" />
          </label>

          <label class="sb-start__field sb-start__field--full">
            <div class="sb-start__label">Extra fuzzer env JSON (optional)</div>
	            <textarea
	              v-model="fuzzerEnvJson"
	              class="sb-start__input sb-start__textarea"
	              rows="6"
	              placeholder='{"SCFUZZBENCH_PROPERTIES_PATH":"..."}'
	            />
	          </label>

          <label class="sb-start__field sb-start__field--full">
            <div class="sb-start__label">Fuzzer variants JSON (optional)</div>
	            <textarea
	              v-model="fuzzerVariantsJson"
	              class="sb-start__input sb-start__textarea"
	              rows="4"
	              placeholder='[{"key":"echidna-2-2-6","base":"echidna","version":"2.2.6"}]'
	            />
	          </label>
        </div>

        <p class="sb-start__hint">
          Use <code>fuzzer_variants</code> to run one fuzzer twice at different revisions. A variant key must start
          with <code>&lt;base&gt;-</code> and is added to <code>fuzzers</code> automatically.
        </p>

        <p class="sb-start__hint">
          Cloud runs build Foundry from the pinned git ref. The <code>--foundry-version</code> release override is
          available only through <code>scripts/local-run.sh</code>.
          <br />
          Note: setting <code>properties_path</code> or <code>fuzzer_env_json</code> causes the workflow to pass a
          complete <code>fuzzer_env</code> map to Terraform (overriding its defaults). Leave these blank unless you know
          you want that.
        </p>
      </div>
    </div>

    <details class="sb-start__preview" open>
      <summary>Request JSON preview</summary>
      <pre><code>{{ requestJson }}</code></pre>
    </details>
  </div>
</template>
