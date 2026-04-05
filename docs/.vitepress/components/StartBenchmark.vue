<script setup lang="ts">
import { computed, ref, watch } from "vue";
import ec2Pricing from "../generated/ec2-pricing.json";

type BenchmarkType = "property" | "optimization";
type Ec2PricingTable = Record<string, number>;
type PreconfiguredTarget = {
  id: string;
  label: string;
  repoUrl: string;
  commit: string;
};

const REPO_OWNER = "Recon-Fuzz";
const REPO_NAME = "scfuzzbench";
const NEW_ISSUE_URL = `https://github.com/${REPO_OWNER}/${REPO_NAME}/issues/new`;
const DEFAULT_FUZZER_ENV_OVERRIDE = JSON.stringify({
  ECHIDNA_TARGET: "tests/recon/CryticTester.sol",
});
const TARGET_REPO_OVERRIDE_KEY = "github.com/recon-fuzz/aave-v4-scfuzzbench";
const PRECONFIGURED_TARGETS: PreconfiguredTarget[] = [
  {
    id: "aave-v4",
    label: "Aave v4",
    repoUrl: "https://github.com/Recon-Fuzz/aave-v4-scfuzzbench",
    commit: "v0.5.6-recon",
  },
  {
    id: "superform-v2-periphery",
    label: "Superform v2-periphery",
    repoUrl: "https://github.com/Recon-Fuzz/superform-v2-periphery-scfuzzbench",
    commit: "dev-recon",
  },
  {
    id: "liquity-v2-governance",
    label: "Liquity v2 Governance",
    repoUrl: "https://github.com/Recon-Fuzz/liquity-V2-gov-scfuzzbench",
    commit: "recon",
  },
  {
    id: "nerite",
    label: "Nerite",
    repoUrl: "https://github.com/Recon-Fuzz/nerite-scfuzzbench",
    commit: "dev-recon",
  },
];
const CUSTOM_TARGET_ID = "custom";

// Defaults are intentionally aligned with the repo's typical local `.env` values.
// Avoid putting anything secret here: this is a fully static site.
const targetRepoUrl = ref(PRECONFIGURED_TARGETS[0].repoUrl);
const targetCommit = ref(PRECONFIGURED_TARGETS[0].commit);
const selectedPreconfiguredTargetId = ref(PRECONFIGURED_TARGETS[0].id);
const isApplyingPreconfiguredTarget = ref(false);

const benchmarkType = ref<BenchmarkType>("property");
const instanceType = ref("c6a.4xlarge");
const instancesPerFuzzer = ref(4);
const timeoutHours = ref(1);

// Dynamically discover fuzzers from committed run scripts.
// In CI tarball checkouts, this naturally reflects the tracked repo content.
const discoveredFuzzerRuns = import.meta.glob("../../../fuzzers/*/run.sh", {
  query: "?raw",
  import: "default",
});

function orderFuzzers(keys: string[]): string[] {
  const unique = Array.from(new Set(keys));
  const preferred = ["echidna", "medusa", "foundry", "echidna-symexec"];
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

const selectableFuzzerKeys = allFuzzerKeys;
const selectedFuzzerKeys = ref<string[]>(
  allFuzzerKeys.filter((name) => name !== "echidna-symexec")
);
const participatingFuzzerKeys = computed(() => {
  const selected = new Set(selectedFuzzerKeys.value);
  return allFuzzerKeys.filter((name) => selected.has(name));
});

// Advanced / optional overrides.
const foundryVersion = ref("");
const foundryGitRepo = ref("https://github.com/0xalpharush/foundry");
const foundryGitRef = ref("fail_on_assert");

const echidnaVersion = ref("");
const medusaVersion = ref("");
const bitwuzlaVersion = ref("");

const gitTokenSsmParameterName = ref("/scfuzzbench/recon/github_token");

const propertiesPath = ref("");
const fuzzerEnvJson = ref(DEFAULT_FUZZER_ENV_OVERRIDE);
const autoOverrideApplied = ref(true);

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

function isDefaultOverrideRepo(url: string): boolean {
  return normalizeRepoUrl(url) === TARGET_REPO_OVERRIDE_KEY;
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
  isApplyingPreconfiguredTarget.value = false;
});

watch([targetRepoUrl, targetCommit], ([nextRepo, nextCommit]) => {
  if (isApplyingPreconfiguredTarget.value) {
    return;
  }
  const matched = findPreconfiguredTarget(nextRepo, nextCommit);
  selectedPreconfiguredTargetId.value = matched ? matched.id : CUSTOM_TARGET_ID;
});

watch(targetRepoUrl, (next) => {
  if (!isDefaultOverrideRepo(next) && autoOverrideApplied.value) {
    fuzzerEnvJson.value = "";
    autoOverrideApplied.value = false;
  }
  if (isDefaultOverrideRepo(next) && !fuzzerEnvJson.value.trim()) {
    fuzzerEnvJson.value = DEFAULT_FUZZER_ENV_OVERRIDE;
    autoOverrideApplied.value = true;
  }
});

watch(fuzzerEnvJson, (next) => {
  if (!autoOverrideApplied.value) {
    return;
  }
  const normalized = next.trim();
  if (!isDefaultOverrideRepo(targetRepoUrl.value) || normalized !== DEFAULT_FUZZER_ENV_OVERRIDE.trim()) {
    autoOverrideApplied.value = false;
  }
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
  const selectedFuzzers = participatingFuzzerKeys.value.length;
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

const requestJson = computed(() => {
  const payload: Record<string, unknown> = {
    target_repo_url: targetRepoUrl.value.trim(),
    target_commit: targetCommit.value.trim(),
    benchmark_type: benchmarkType.value,
    instance_type: instanceType.value.trim(),
    instances_per_fuzzer: instancesPerFuzzer.value,
    timeout_hours: timeoutHours.value,
    fuzzers: participatingFuzzerKeys.value,

    foundry_version: foundryVersion.value.trim(),
    foundry_git_repo: foundryGitRepo.value.trim(),
    foundry_git_ref: foundryGitRef.value.trim(),

    echidna_version: echidnaVersion.value.trim(),
    medusa_version: medusaVersion.value.trim(),
    bitwuzla_version: bitwuzlaVersion.value.trim(),

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
    "- `target_commit` may be a commit SHA, tag, or branch name.",
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
              {{ target.label }} ({{ target.commit }})
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

        <label class="sb-start__field sb-start__field--full">
          <div class="sb-start__label">Fuzzers</div>
          <div class="sb-start__fuzzers">
            <label
              v-for="fuzzer in selectableFuzzerKeys"
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
            <div class="sb-start__label">Foundry version override (optional)</div>
            <input v-model="foundryVersion" class="sb-start__input" type="text" placeholder="e.g. v1.6.0-rc1" />
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
            <div class="sb-start__label">Medusa version override (optional)</div>
            <input v-model="medusaVersion" class="sb-start__input" type="text" placeholder="e.g. 1.4.1" />
          </label>

          <label class="sb-start__field">
            <div class="sb-start__label">Bitwuzla version override (optional)</div>
            <input v-model="bitwuzlaVersion" class="sb-start__input" type="text" placeholder="e.g. 0.8.2" />
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
        </div>

        <p class="sb-start__hint">
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
