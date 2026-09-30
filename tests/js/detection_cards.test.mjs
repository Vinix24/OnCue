/**
 * dashboard/js/pain-points.js — confidence tiering on the detection rows.
 *
 * Before #205 PainPointRouter's keyword fast path returned a hardcoded
 * confidence=0.95/tier="high" for every match, so rendering the tier would
 * have rendered a constant. #205 made the score proportional to real overlap
 * and derived the tier from the configured thresholds, so a weak match is now
 * genuinely different from a strong one and must not look identical to it.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, el, settle } from "./dom_stub.mjs";
import { buildDetectorStatusFixture, detectorStatusPayload } from "./dashboard_fixture.mjs";

async function bootPanel() {
  const env = createEnvironment();
  const status = buildDetectorStatusFixture(env);
  const panel = el("div", { id: "pain-points-panel", className: "pain-points" });
  panel.appendChild(el("div", { className: "pain-points-empty" }));
  env.mount(panel);

  env.load("dashboard/js/detector-status.js");
  env.load("dashboard/js/pain-points.js");
  await settle();

  const statusSocket = env.sockets.find((socket) => socket.url.endsWith("/ws/detector-status"));
  const painPointSocket = env.sockets.find((socket) => socket.url.endsWith("/ws/pain-points"));
  assert.ok(painPointSocket, "pain-points.js must subscribe to /ws/pain-points");
  return { env, panel, statusSocket, painPointSocket, ...status };
}

const painPoint = (confidence) => ({
  type: "pain_point",
  category: "handmatig_werk",
  confidence,
  trigger_phrase: "we doen dat allemaal met de hand",
  timestamp_ms: 61000,
  case_matched: false,
  case_id: null,
});

test("a strong match is marked high", async () => {
  const { panel, painPointSocket } = await bootPanel();

  painPointSocket.deliver(painPoint(0.93));

  const card = panel.querySelector(".pain-point-card");
  assert.ok(card, "a detection must render a card");
  assert.equal(card.classList.contains("is-high"), true);
  assert.equal(card.classList.contains("is-uncertain"), false);
  assert.equal(card.title, "Sterke match");
});

test("a weak match does not look like a strong one", async () => {
  const { panel, painPointSocket } = await bootPanel();

  painPointSocket.deliver(painPoint(0.62));

  const card = panel.querySelector(".pain-point-card");
  assert.equal(card.classList.contains("is-uncertain"), true);
  assert.equal(card.classList.contains("is-high"), false);
  assert.equal(card.title, "Zwakke match, niet bevestigd");
});

test("the tier follows the thresholds the running detector reported", async () => {
  const { panel, statusSocket, painPointSocket } = await bootPanel();

  statusSocket.deliver(detectorStatusPayload({ thresholds: { low: 0.4, high: 0.55 } }));
  painPointSocket.deliver(painPoint(0.62));

  const card = panel.querySelector(".pain-point-card");
  assert.equal(
    card.classList.contains("is-high"),
    true,
    "0.62 is a high match on a run configured with high=0.55",
  );
});

test("the confidence percentage is still rendered as before", async () => {
  const { panel, painPointSocket } = await bootPanel();

  painPointSocket.deliver(painPoint(0.62));

  const confidence = panel.querySelector(".pain-point-confidence");
  assert.equal(confidence.textContent, "62%");
});

test("a card with no usable confidence is left untiered rather than guessed", async () => {
  const { panel, painPointSocket } = await bootPanel();

  painPointSocket.deliver({ ...painPoint(0.5), confidence: null });

  const card = panel.querySelector(".pain-point-card");
  assert.equal(card.classList.contains("is-high"), false);
  assert.equal(card.classList.contains("is-uncertain"), false);
});
