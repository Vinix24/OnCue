/**
 * dashboard/js/detector-status.js — the strip that answers, mid-call,
 * "is the AI listening or is something broken?".
 *
 * The module is driven exactly as the hub drives it: a detector_status payload
 * pushed into the stubbed /ws/detector-status socket, and the rendered DOM read
 * back. Copy comes from the real dashboard/i18n/nl.json, so a key missing from
 * the catalog fails here instead of rendering as a raw dotted key on screen.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, settle } from "./dom_stub.mjs";
import { buildDetectorStatusFixture, detectorStatusPayload } from "./dashboard_fixture.mjs";

async function bootStatus() {
  const env = createEnvironment();
  const fixture = buildDetectorStatusFixture(env);
  env.load("dashboard/js/detector-status.js");
  await settle();
  const socket = env.sockets.find((candidate) => candidate.url.endsWith("/ws/detector-status"));
  assert.ok(socket, "detector-status.js must subscribe to /ws/detector-status");
  return { env, socket, ...fixture };
}

test("before any payload the strip reads as not started, not as broken", async () => {
  const { host, label } = await bootStatus();

  assert.equal(host.dataset.state, "waiting");
  assert.equal(label.textContent, "Detectie nog niet actief");
  assert.equal(host.classList.contains("is-idle"), true);
  assert.equal(host.classList.contains("is-warn"), false, "not-started must not read as a fault");
});

test("alive with nothing heard yet reads as listening", async () => {
  const { socket, host, label } = await bootStatus();

  socket.deliver(detectorStatusPayload({ state: "listening", uptimeMs: 3000 }));

  assert.equal(host.dataset.state, "listening");
  assert.equal(host.classList.contains("is-live"), true);
  assert.equal(label.textContent, "Luistert, nog niets binnen");
});

test("the summary names utterances seen and detections surfaced", async () => {
  const { socket, host, label } = await bootStatus();

  socket.deliver(detectorStatusPayload({ counts: { received: 42, classified: 12, dispatched: 3 } }));

  assert.equal(host.dataset.state, "active");
  assert.match(label.textContent, /42/);
  assert.match(label.textContent, /3/);
  assert.equal(label.textContent, "Luistert · 42 gehoord · 3 gedetecteerd");
});

test("nothing detected is explained by the counter that ate the chunks", async () => {
  const { socket, toggle, detail } = await bootStatus();

  socket.deliver(detectorStatusPayload({
    counts: { received: 30, skipped_not_prospect: 28, buffered_below_min: 2 },
  }));
  toggle.click();

  assert.equal(detail.classList.contains("hidden"), false, "the reason must be reachable");
  assert.match(detail.textContent, /jouw spraak/, "the dominant filter must be named");
  assert.match(detail.textContent, /28/, "the counter itself must be shown");
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
});

test("a below-threshold sweep is named as such, not blamed on the prospect", async () => {
  const { socket, toggle, detail } = await bootStatus();

  socket.deliver(detectorStatusPayload({
    counts: { received: 30, classified: 9, dropped_low_confidence: 9 },
  }));
  toggle.click();

  assert.match(detail.textContent, /betrouwbaarheidsdrempel/);
});

test("every counter from the detector loop is listed in the breakdown", async () => {
  const { socket, toggle, detail } = await bootStatus();

  socket.deliver(detectorStatusPayload({
    counts: {
      received: 11,
      skipped_not_prospect: 1,
      buffered_below_min: 2,
      debounced: 3,
      classified: 4,
      dropped_none: 5,
      dropped_low_confidence: 6,
      dispatched: 7,
    },
  }));
  toggle.click();

  const counts = detail.querySelector(".detector-status-counts");
  assert.ok(counts, "the breakdown table must render");
  // Eight counters, each a <dt>/<dd> pair.
  assert.equal(counts.children.length, 16);
  ["11", "1", "2", "3", "4", "5", "6", "7"].forEach((value) => {
    assert.ok(
      counts.children.some((child) => child.tagName === "DD" && child.textContent === value),
      `counter value ${value} missing from the breakdown`,
    );
  });
});

test("silence from the detector is surfaced as a fault, not as calm", async () => {
  const { env, socket, host, label } = await bootStatus();

  socket.deliver(detectorStatusPayload({ counts: { received: 12, dispatched: 1 } }));
  assert.equal(host.dataset.state, "active");

  // Three publish intervals of silence: the detector process is gone.
  env.advanceClock(16000);
  env.tickIntervals();

  assert.equal(host.dataset.state, "stale");
  assert.equal(host.classList.contains("is-warn"), true);
  assert.equal(label.textContent, "Geen signaal van de detectie");
});

test("running but receiving nothing at all is a fault, not 'no pain points yet'", async () => {
  const { socket, host, label } = await bootStatus();

  socket.deliver(detectorStatusPayload({ state: "listening", uptimeMs: 90000 }));

  assert.equal(host.dataset.state, "no_input");
  assert.equal(host.classList.contains("is-warn"), true);
  assert.equal(label.textContent, "Luistert, maar er komt niets binnen");
});

test("the closing summary leaves the strip stopped, not alarmed", async () => {
  const { socket, host, label } = await bootStatus();

  socket.deliver(detectorStatusPayload({ state: "stopped", counts: { received: 40, dispatched: 4 } }));

  assert.equal(host.dataset.state, "stopped");
  assert.equal(host.classList.contains("is-warn"), false);
  assert.equal(label.textContent, "Detectie gestopt");
});

test("resetting for a new call clears the previous call's counters", async () => {
  const { env, socket, host } = await bootStatus();

  socket.deliver(detectorStatusPayload({ counts: { received: 40, dispatched: 4 } }));
  assert.equal(host.dataset.state, "active");

  env.context.resetDetectorStatus();

  assert.equal(host.dataset.state, "waiting");
});

test("the confidence tier follows the detector's own configured thresholds", async () => {
  const { env, socket } = await bootStatus();

  // Defaults before any payload mirror DetectorConfig (0.85 / 0.50).
  assert.equal(env.context.detectorConfidenceTier(0.9), "high");
  assert.equal(env.context.detectorConfidenceTier(0.6), "uncertain");
  assert.equal(env.context.detectorConfidenceTier(undefined), "unknown");
  assert.equal(env.context.detectorConfidenceTier(72), "uncertain", "percent scale must be normalised");

  socket.deliver(detectorStatusPayload({ thresholds: { low: 0.4, high: 0.55 } }));

  assert.equal(
    env.context.detectorConfidenceTier(0.6),
    "high",
    "a run with a lower high-threshold must tier the same score as high",
  );
});

test("detector-status.js renders no untranslated i18n keys", async () => {
  const { env, socket, toggle } = await bootStatus();

  socket.deliver(detectorStatusPayload({ counts: { received: 5, buffered_below_min: 5 } }));
  toggle.click();

  assert.deepEqual(env.missingI18nKeys, []);
});
