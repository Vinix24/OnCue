/**
 * dashboard/js/setup.js — the module-checkbox choke point.
 *
 * #206 introduced writeModuleCheckboxes(modules, {deliberate}) so an automatic
 * preset application on page load can never silently overwrite an operator's
 * module choices, and it verified that with a throwaway harness that was then
 * discarded. These tests are that harness, committed: the guard, and the
 * screen-mode hole #206's own report flagged and left open, are now covered.
 *
 * Everything is driven the way a browser drives it: seed localStorage, stub
 * /api/presets, load the real setup.js, and read the resulting checkbox state.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, settle } from "./dom_stub.mjs";
import { buildSetupFixture } from "./dashboard_fixture.mjs";

const STORAGE_KEY = "sales-copilot-setup";

/** config/presets.yaml's first preset turns pain_points off on purpose. */
const DISCOVERY_PRESET = {
  name: "discovery",
  screen_mode: "single",
  modules: {
    talk_time: true,
    transcript: true,
    pain_points: false,
    presentation: false,
    post_call_report: true,
  },
};

const DUAL_SCREEN_PRESET = {
  name: "demo",
  screen_mode: "dual",
  modules: {
    talk_time: true,
    transcript: true,
    pain_points: true,
    presentation: true,
    post_call_report: true,
  },
};

async function bootSetup({ storage = {}, presets = [DISCOVERY_PRESET] } = {}) {
  const env = createEnvironment({
    storage,
    routes: { "/api/presets": presets, "/api/llm-models": { providers: {} } },
  });
  const fixture = buildSetupFixture(env);
  env.load("dashboard/js/setup.js");
  await settle();
  return { env, ...fixture };
}

const storedConfig = (modules, screenMode = "dual") => ({
  [STORAGE_KEY]: JSON.stringify({
    preset_name: "sales",
    screen_mode: screenMode,
    modules,
    llm: { provider: "", model: "" },
    transcript: { backend: "whisper.cpp", transcribe_self_live: false },
    prospect: { company: "", industry: "" },
  }),
});

test("a fresh browser lets the auto-applied preset establish the module state", async () => {
  const { moduleInputs } = await bootSetup();

  assert.equal(
    moduleInputs.pain_points.checked,
    false,
    "with nothing stored, the first preset must be allowed to write once",
  );
});

test("a non-deliberate preset apply cannot overwrite an established choice", async () => {
  const { moduleInputs } = await bootSetup({
    storage: storedConfig({
      talk_time: true,
      transcript: true,
      pain_points: true,
      presentation: false,
      post_call_report: true,
    }),
  });

  assert.equal(
    moduleInputs.pain_points.checked,
    true,
    "the stored operator choice must survive the auto-applied discovery preset",
  );
});

test("a deliberate preset click may still switch detection off", async () => {
  const { presetBar, moduleInputs } = await bootSetup({
    storage: storedConfig({
      talk_time: true,
      transcript: true,
      pain_points: true,
      presentation: false,
      post_call_report: true,
    }),
  });

  const pill = presetBar.querySelector(".preset-pill");
  assert.ok(pill, "the preset bar must render a pill for each preset");
  pill.click();

  assert.equal(
    moduleInputs.pain_points.checked,
    false,
    "clicking the discovery preset is deliberate and must be honoured",
  );
});

test("a non-deliberate preset apply cannot reset presentation via setScreenMode", async () => {
  // The gap #206 left open: setScreenMode("single") clears the presentation
  // checkbox as a side effect, and that call was deliberately left ungated, so
  // an automatic preset apply on load could still wipe a stored
  // presentation:true straight past the guard sitting next to it.
  const { moduleInputs } = await bootSetup({
    storage: storedConfig({
      talk_time: true,
      transcript: true,
      pain_points: true,
      presentation: true,
      post_call_report: true,
    }, "dual"),
  });

  assert.equal(
    moduleInputs.presentation.checked,
    true,
    "a single-screen preset applied non-deliberately must not clear presentation",
  );
});

test("a deliberate single-screen choice still clears and disables presentation", async () => {
  const { screenToggle, moduleInputs } = await bootSetup({
    storage: storedConfig({
      talk_time: true,
      transcript: true,
      pain_points: true,
      presentation: true,
      post_call_report: true,
    }, "dual"),
  });

  const singleButton = screenToggle.querySelectorAll(".toggle-button")
    .find((button) => button.dataset.mode === "single");
  singleButton.click();

  assert.equal(moduleInputs.presentation.checked, false, "one screen cannot drive a second one");
  assert.equal(moduleInputs.presentation.disabled, true, "the input must be disabled too");
});

test("a fresh browser applying a single-screen preset ends with presentation off", async () => {
  // The ordering trap in the fix: resolving the guard per call would let the
  // preset's own module write flip modulesEstablished and then lock out the
  // screen-mode side effect belonging to the same apply.
  const { moduleInputs } = await bootSetup({
    presets: [{
      ...DISCOVERY_PRESET,
      modules: { ...DISCOVERY_PRESET.modules, presentation: true },
    }],
  });

  assert.equal(
    moduleInputs.presentation.checked,
    false,
    "the single-screen constraint must win over the preset's own presentation:true",
  );
  assert.equal(moduleInputs.presentation.disabled, true);
});

test("a dual-screen preset re-enables the presentation input", async () => {
  const { moduleInputs } = await bootSetup({ presets: [DUAL_SCREEN_PRESET] });

  assert.equal(moduleInputs.presentation.checked, true);
  assert.equal(moduleInputs.presentation.disabled, false);
});

test("the detection-off indicator tracks the live pain_points checkbox", async () => {
  const off = await bootSetup();
  assert.equal(
    off.indicator.classList.contains("hidden"),
    false,
    "detection off must show the standing indicator",
  );

  const on = await bootSetup({
    storage: storedConfig({
      talk_time: true,
      transcript: true,
      pain_points: true,
      presentation: false,
      post_call_report: true,
    }),
  });
  assert.equal(
    on.indicator.classList.contains("hidden"),
    true,
    "detection on must hide the indicator",
  );
});

test("setup.js renders no untranslated i18n keys", async () => {
  const { env } = await bootSetup();
  assert.deepEqual(env.missingI18nKeys, []);
});
