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
    setup(props, context) {
      const state = component.setup(props, context);
      configure(state);
      payload = JSON.parse(state.requestJson.value);
      issueUrl = state.issueUrl.value;
      return state;
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
