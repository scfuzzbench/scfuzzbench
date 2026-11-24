import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";

let server;
let component;
let defaults;
const VERIFIED_PARAMETER = "/scfuzzbench/test/verified_actions_token";

before(async () => {
  server = await createServer({
    configFile: false,
    plugins: [vue()],
    server: { middlewareMode: true },
    appType: "custom",
    logLevel: "error",
  });
  ({ default: defaults } = await server.ssrLoadModule(
    "/docs/.vitepress/generated/benchmark-defaults.json"
  ));
  ({ default: component } = await server.ssrLoadModule(
    "/docs/.vitepress/components/StartBenchmark.vue"
  ));
});

after(async () => {
  await server?.close();
});

async function renderRequest(configure = () => {}, defaultParameter = VERIFIED_PARAMETER) {
  defaults.echidna_ci_token_ssm_parameter_name = defaultParameter;
  let payload;
  let issueUrl;
  const html = await renderToString(createSSRApp({
    ...component,
    async setup(props, context) {
      const state = component.setup(props, context);
      try {
        await configure(state);
        payload = JSON.parse(state.requestJson.value);
        issueUrl = state.issueUrl.value;
        return state;
      } finally {
        // These tests explicitly await lookups instead of waiting for debounce.
        clearTimeout(state.echidnaResolveTimer);
      }
    },
  }));
  const link = html.match(/<a\b[^>]*>\s*Open GitHub request issue\s*<\/a>/)?.[0];
  assert.ok(link, "request action should be rendered");
  return { payload, issueUrl, link };
}

// The form state after resolving the CI build selected in issue #300.
function selectCiBuild(state) {
  state.echidnaCiRepo.value = "https://github.com/crytic/echidna";
  state.echidnaCiRunId.value = "12345";
  state.echidnaCiArtifactName.value = "echidna-Linux";
  state.echidnaCiArtifactSha256.value = "a".repeat(64);
  state.echidnaCiCommit.value = "b".repeat(40);
}

function assertRequestEnabled(result) {
  assert.match(result.link, /href="https:\/\/github.com\//);
  assert.doesNotMatch(result.link, /aria-disabled="true"/);
  const body = new URL(result.issueUrl).searchParams.get("body");
  const json = body.match(/```json\n([\s\S]*?)\n```/)[1];
  assert.deepEqual(JSON.parse(json), result.payload);
}

function assertRequestDisabled(result) {
  assert.equal(result.issueUrl, undefined);
  assert.doesNotMatch(result.link, /\bhref=/);
  assert.match(result.link, /aria-disabled="true"/);
}

test("release-only requests omit CI authentication", async () => {
  const result = await renderRequest();
  assert.equal(result.payload.echidna_ci_token_ssm_parameter_name, "");
  assert.equal(result.payload.echidna_ci_token_kms_key_arn, "");
  assert.equal(result.payload.git_token_ssm_parameter_name, "/scfuzzbench/recon/github_token");
  assertRequestEnabled(result);
});

test("CI and release comparisons include the configured token parameter", async () => {
  const result = await renderRequest((state) => {
    selectCiBuild(state);
    state.fuzzerVariantsJson.value = JSON.stringify([
      { key: "echidna-release-2-4-0", base: "echidna", version: "2.4.0" },
    ]);
  });
  assert.equal(result.payload.echidna_ci_token_ssm_parameter_name, VERIFIED_PARAMETER);
  assert.ok(result.payload.fuzzers.includes("echidna-release-2-4-0"));
  assertRequestEnabled(result);
});

test("manually entered CI builds also receive the default parameter", async () => {
  const result = await renderRequest(selectCiBuild);
  assert.equal(result.payload.echidna_ci_token_ssm_parameter_name, VERIFIED_PARAMETER);
  assertRequestEnabled(result);
});

test("CI requests preserve custom token and KMS settings", async () => {
  const result = await renderRequest((state) => {
    selectCiBuild(state);
    state.echidnaCiTokenSsmParameterName.value = " /scfuzzbench/custom/actions_token ";
    state.echidnaCiTokenKmsKeyArn.value = " arn:aws:kms:us-east-1:123456789012:key/example ";
  });
  assert.equal(result.payload.echidna_ci_token_ssm_parameter_name, "/scfuzzbench/custom/actions_token");
  assert.equal(result.payload.echidna_ci_token_kms_key_arn, "arn:aws:kms:us-east-1:123456789012:key/example");
  assertRequestEnabled(result);
});

test("clearing the token parameter blocks CI issue creation", async () => {
  const result = await renderRequest((state) => {
    selectCiBuild(state);
    state.echidnaCiTokenSsmParameterName.value = "   ";
  });
  assertRequestDisabled(result);
});

test("unavailable defaults block CI requests but allow releases and maintainer overrides", async () => {
  const ci = await renderRequest(selectCiBuild, "");
  assert.equal(ci.payload.echidna_ci_token_ssm_parameter_name, "");
  assertRequestDisabled(ci);
  assertRequestEnabled(await renderRequest(() => {}, ""));
  const override = await renderRequest((state) => {
    selectCiBuild(state);
    state.echidnaCiTokenSsmParameterName.value = "/scfuzzbench/custom/actions_token";
  }, "");
  assert.equal(override.payload.echidna_ci_token_ssm_parameter_name, "/scfuzzbench/custom/actions_token");
  assertRequestEnabled(override);
});

test("invalid token parameter paths block CI issue creation", async () => {
  for (const parameter of ["github_token", "/elsewhere/token", "/scfuzzbench/", `/scfuzzbench/${"a".repeat(200)}`]) {
    const result = await renderRequest((state) => {
      selectCiBuild(state);
      state.echidnaCiTokenSsmParameterName.value = parameter;
    });
    assertRequestDisabled(result);
  }
});

test("returning to release-only mode omits saved CI token and KMS settings", async () => {
  const result = await renderRequest((state) => {
    selectCiBuild(state);
    state.echidnaCiTokenSsmParameterName.value = "/scfuzzbench/custom/actions_token";
    state.echidnaCiTokenKmsKeyArn.value = "arn:aws:kms:us-east-1:123456789012:key/example";
    state.clearEchidnaBuildFields();
  });
  assert.equal(result.payload.echidna_ci_token_ssm_parameter_name, "");
  assert.equal(result.payload.echidna_ci_token_kms_key_arn, "");
  assertRequestEnabled(result);
});

function mockEchidnaApi(context, { onlyTestsuite = false, beforeRequest = async () => {} } = {}) {
  context.mock.method(globalThis, "fetch", async (url) => {
    await beforeRequest(url);
    const path = new URL(url).pathname;
    let data;
    if (path.endsWith("/commits/master")) {
      data = { sha: "a".repeat(40) };
    } else if (path.endsWith("/pulls/1654")) {
      data = { head: { sha: "b".repeat(40) } };
    } else if (path.endsWith("/actions/runs")) {
      const id = new URL(url).searchParams.get("head_sha") === "a".repeat(40) ? 1 : 2;
      data = { workflow_runs: [{ id, status: "completed", conclusion: "success" }] };
    } else if (/\/actions\/runs\/[12]\/artifacts$/.test(path)) {
      data = { artifacts: [
        { name: "echidna-testsuite-Linux", expired: false, digest: `sha256:${"c".repeat(64)}` },
        ...onlyTestsuite ? [] : [
          { name: "echidna-Linux", expired: false, digest: `sha256:${"d".repeat(64)}` },
        ],
      ] };
    } else {
      throw new Error(`unexpected API request: ${url}`);
    }
    return { ok: true, status: 200, json: async () => data };
  });
}

test("master and PR selection resolves to fuzzer archives in the generated issue", async (context) => {
  mockEchidnaApi(context);
  const result = await renderRequest(async (state) => {
    state.selectedEchidnaOptions.value = ["master", "pull"];
    state.echidnaPullNumber.value = "1654";
    await state.resolveEchidnaSelection();
  });
  assertRequestEnabled(result);
  assert.equal(result.payload.echidna_ci_artifact_name, "echidna-Linux");
  assert.equal(result.payload.echidna_ci_token_ssm_parameter_name, VERIFIED_PARAMETER);
  assert.equal(result.payload.fuzzer_variants[0].key, "echidna-pr-1654");
  assert.equal(result.payload.fuzzer_variants[0].ci.artifact_name, "echidna-Linux");
  assert.equal(result.payload.fuzzer_variants[0].ci.artifact_sha256, "d".repeat(64));
});

test("testsuite-only lookup failure disables submission instead of creating a release request", async (context) => {
  mockEchidnaApi(context, { onlyTestsuite: true });
  const result = await renderRequest(async (state) => {
    state.selectedEchidnaOptions.value = ["pull"];
    state.echidnaPullNumber.value = "1654";
    await state.resolveEchidnaSelection();
    assert.match(state.echidnaResolveError.value, /no successful run.*supported/);
  });
  assertRequestDisabled(result);
});

test("pending lookups and incomplete PR numbers disable submission", async () => {
  assertRequestDisabled(await renderRequest((state) => {
    state.selectedEchidnaOptions.value = ["master"];
  }));
  for (const number of ["", "0", "abc", "-1", "1654"]) {
    assertRequestDisabled(await renderRequest((state) => {
      state.selectedEchidnaOptions.value = ["master", "pull"];
      state.echidnaPullNumber.value = number;
    }));
  }
});

test("changing the PR immediately disables a previously resolved request", async (context) => {
  mockEchidnaApi(context);
  const result = await renderRequest(async (state) => {
    state.selectedEchidnaOptions.value = ["master", "pull"];
    state.echidnaPullNumber.value = "1654";
    await state.resolveEchidnaSelection();
    assert.ok(state.issueUrl.value);
    state.echidnaPullNumber.value = "1655";
    assert.equal(state.echidnaCiCommit.value, "");
  });
  assertRequestDisabled(result);
});

test("late lookup success or failure cannot overwrite a return to release-only mode", async (context) => {
  for (const fail of [false, true]) {
    let finishRequest;
    let notifyStarted;
    const requestPending = new Promise((resolve) => { finishRequest = resolve; });
    const requestStarted = new Promise((resolve) => { notifyStarted = resolve; });
    mockEchidnaApi(context, { beforeRequest: async (url) => {
      if (url.endsWith("/commits/master")) {
        notifyStarted();
        await requestPending;
        if (fail) throw new Error("late network failure");
      }
    } });
    const result = await renderRequest(async (state) => {
      state.selectedEchidnaOptions.value = ["master"];
      const lookup = state.resolveEchidnaSelection();
      await requestStarted;
      state.selectedEchidnaOptions.value = ["release"];
      await state.resolveEchidnaSelection();
      finishRequest();
      await lookup;
      assert.equal(state.echidnaResolveError.value, "");
    });
    assertRequestEnabled(result);
    assert.equal(result.payload.echidna_ci_repo, "");
    assert.equal(result.payload.echidna_ci_token_ssm_parameter_name, "");
    assert.deepEqual(result.payload.fuzzer_variants, []);
  }
});
