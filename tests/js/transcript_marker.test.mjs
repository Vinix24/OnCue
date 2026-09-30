/**
 * dashboard/js/transcript.js — the audio-loss marker row.
 *
 * A warning in the coaching panel scrolls away; the gap it describes does not.
 * The marker has to sit in the transcript itself, at the timestamp the audio
 * stopped, or reading back suggests the prospect went quiet. That is the wrong
 * conclusion a reader drew from the 2026-09-10 call before this existed.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, settle } from "./dom_stub.mjs";
import { buildTranscriptFixture } from "./dashboard_fixture.mjs";

const MARKER_TEXT = "[audio ontbreekt vanaf hier: de prospect-tap levert geen signaal meer.]";

async function bootTranscript() {
  const env = createEnvironment();
  const fixture = buildTranscriptFixture(env);
  env.load("dashboard/js/transcript.js");
  await settle();
  const socket = env.sockets.find((candidate) => candidate.url.endsWith("/ws/transcript"));
  assert.ok(socket, "transcript.js must subscribe to /ws/transcript");
  return { env, socket, ...fixture };
}

const marker = (startMs = 960000) => ({
  type: "transcript_marker",
  marker: "audio_signal_lost",
  stream: "prospect",
  speaker: "system",
  text: MARKER_TEXT,
  start_ms: startMs,
  end_ms: startMs,
});

const line = (text, speaker, startMs) => ({
  type: "transcript",
  text,
  speaker,
  start_ms: startMs,
  end_ms: startMs + 900,
  is_final: true,
});

test("a marker renders as its own row, not as something somebody said", async () => {
  const { socket, panel } = await bootTranscript();

  socket.deliver(marker());

  assert.equal(panel.children.length, 1);
  const row = panel.children[0];
  assert.equal(row.classList.contains("transcript-marker"), true);
  assert.equal(row.querySelector(".transcript-text").textContent, MARKER_TEXT);
  // No speaker role: this is not one of the two sides of the conversation.
  assert.equal(row.querySelector(".transcript-speaker").dataset.role, undefined);
  assert.equal(row.querySelector(".transcript-speaker").textContent, "AUDIO WEG");
  assert.equal(row.querySelector(".transcript-time").textContent, "16:00");
});

test("the marker lands where the audio stopped, not at the end of the list", async () => {
  const { socket, panel } = await bootTranscript();

  socket.deliver(line("Hoeveel offertes maakt u per week?", "self", 900000));
  socket.deliver(line("Dat weet ik niet uit mijn hoofd.", "prospect", 1200000));
  socket.deliver(marker(960000));

  const texts = panel.children.map((row) => row.querySelector(".transcript-text").textContent);
  assert.deepEqual(texts, [
    "Hoeveel offertes maakt u per week?",
    MARKER_TEXT,
    "Dat weet ik niet uit mijn hoofd.",
  ]);
});

test("swapping the two speakers leaves the marker alone", async () => {
  const { env, socket, panel } = await bootTranscript();

  socket.deliver(line("Goeiemiddag.", "self", 0));
  socket.deliver(marker(960000));
  env.context.swapTranscriptSpeakers();

  const [spoken, markerRow] = panel.children;
  assert.equal(spoken.querySelector(".transcript-speaker").textContent, "Prospect");
  assert.equal(markerRow.querySelector(".transcript-speaker").textContent, "AUDIO WEG");
  assert.equal(markerRow.querySelector(".transcript-speaker").classList.contains("marker"), true);
});

test("a marker with no text is dropped instead of rendering an empty row", async () => {
  const { socket, panel } = await bootTranscript();

  socket.deliver({ type: "transcript_marker", text: "   ", start_ms: 10 });

  assert.equal(panel.children.length, 0);
});
