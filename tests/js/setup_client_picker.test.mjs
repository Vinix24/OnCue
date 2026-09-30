/**
 * dashboard/js/setup.js -- the klantmap-als-eenheid D2 client picker.
 *
 * Replaces the old free-typed "Prospect bedrijf" / "Industry" fields and the
 * "Klantdossier" checkbox with one <select> fed by GET /api/v1/clients, plus a
 * cloud-sync warning banner from that same response. "Server leidt af, dashboard
 * kiest alleen": the only client-related thing the dashboard now sends in the
 * start_call payload is client_slug (or nothing, "geen klant").
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, settle } from "./dom_stub.mjs";
import { buildSetupFixture } from "./dashboard_fixture.mjs";

const STORAGE_KEY = "sales-copilot-setup";

const CLIENTS_RESPONSE = {
  clients: [
    { slug: "acme-corp", bedrijf: "Acme Corp B.V." },
    { slug: "beta-nv", bedrijf: "beta-nv" },
  ],
  cloud_sync_warning: null,
};

async function bootSetup({ storage = {}, clientsResponse = CLIENTS_RESPONSE } = {}) {
  const env = createEnvironment({
    storage,
    routes: {
      "/api/presets": [],
      "/api/llm-models": { providers: {} },
      "/api/v1/clients": clientsResponse,
    },
  });
  const fixture = buildSetupFixture(env);
  env.load("dashboard/js/setup.js");
  await settle();
  return { env, ...fixture };
}

test("the client picker always has a 'Geen klant' option first", async () => {
  const { clientSelect } = await bootSetup();

  const options = clientSelect.children;
  assert.equal(options[0].value, "");
  assert.equal(options[0].textContent, "Geen klant");
});

test("the client picker renders one option per client, labelled with bedrijf", async () => {
  const { clientSelect } = await bootSetup();

  const options = clientSelect.children;
  assert.equal(options.length, 3, "Geen klant + 2 clients");
  assert.equal(options[1].value, "acme-corp");
  assert.equal(options[1].textContent, "Acme Corp B.V.");
  assert.equal(options[2].value, "beta-nv");
  assert.equal(options[2].textContent, "beta-nv", "falls back to the slug when bedrijf is absent");
});

test("saveConfig writes client_slug from the picker and omits prospect/dossier fields", async () => {
  const env = createEnvironment({
    routes: {
      "/api/presets": [],
      "/api/llm-models": { providers: {} },
      "/api/v1/clients": CLIENTS_RESPONSE,
    },
  });
  const { clientSelect } = buildSetupFixture(env);
  env.load("dashboard/js/setup.js");
  await settle();

  clientSelect.value = "acme-corp";
  clientSelect.fire("change");
  await settle();

  const stored = JSON.parse(env.storage[STORAGE_KEY]);
  assert.equal(stored.client_slug, "acme-corp");
  assert.equal(stored.prospect, undefined);
  assert.equal(stored.dossier_opt_in, undefined);
});

test("no client selected persists client_slug as null", async () => {
  const env = createEnvironment({
    routes: {
      "/api/presets": [],
      "/api/llm-models": { providers: {} },
      "/api/v1/clients": CLIENTS_RESPONSE,
    },
  });
  const { clientSelect } = buildSetupFixture(env);
  env.load("dashboard/js/setup.js");
  await settle();

  clientSelect.value = "";
  clientSelect.fire("change");
  await settle();

  const stored = JSON.parse(env.storage[STORAGE_KEY]);
  assert.equal(stored.client_slug, null);
});

test("a stored client_slug re-selects the matching option once clients have loaded", async () => {
  const { clientSelect } = await bootSetup({
    storage: {
      [STORAGE_KEY]: JSON.stringify({
        preset_name: "sales",
        screen_mode: "dual",
        modules: {},
        llm: { provider: "", model: "" },
        transcript: { backend: "whisper.cpp", transcribe_self_live: false },
        client_slug: "beta-nv",
      }),
    },
  });

  assert.equal(clientSelect.value, "beta-nv");
});

test("the cloud-sync warning banner is hidden when the API reports none", async () => {
  const { cloudSyncWarning } = await bootSetup();

  assert.equal(cloudSyncWarning.classList.contains("hidden"), true);
  assert.equal(cloudSyncWarning.textContent, "");
});

test("the cloud-sync warning banner shows the API's message and unhides", async () => {
  const { cloudSyncWarning } = await bootSetup({
    clientsResponse: {
      clients: [],
      cloud_sync_warning: {
        kind: "icloud_drive",
        message: "Deze klantmap staat in iCloud Drive.",
      },
    },
  });

  assert.equal(cloudSyncWarning.classList.contains("hidden"), false);
  assert.equal(cloudSyncWarning.textContent, "Deze klantmap staat in iCloud Drive.");
});

test("setup.js renders no untranslated i18n keys for the client picker", async () => {
  const { env } = await bootSetup();
  assert.deepEqual(env.missingI18nKeys, []);
});
