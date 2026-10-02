/**
 * dashboard/js/report.js -- "the report is being made" and the enriched report.
 *
 * After report_ready the dashboard says the summary and corrections are being prepared. When
 * report_enriched arrives it reads the local report through the token-protected endpoint (the
 * event carries no text), hides the status line and shows summary, overview, keywords, action
 * items and the term corrections. Without report_enriched the line turns neutral after six
 * minutes. The Markdown export carries the enriched fields.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, el, settle } from "./dom_stub.mjs";

const SIX_MINUTES_MS = 6 * 60 * 1000;

const STORED = {
  session_id: "s-1",
  gesprek_gevoerd: true,
  short_summary: "Klant wil sneller offreren.",
  overview: "Eerst de pijn, daarna de planning.",
  keywords: ["offertes", "planning"],
  action_items: ["Offerte sturen", "Demo plannen"],
  term_corrections: [
    { segment_index: 0, source: "Team Leader", occurrence: 1, target: "Teamleader", reason: "" },
  ],
  term_corrections_pii_limited: 0,
};

const ENRICHED_EVENT = {
  type: "report_enriched",
  session_id: "s-1",
  gesprek_gevoerd: true,
  junk: false,
  junk_reason: null,
  term_correction_count: 1,
  term_corrections_pii_limited: 0,
};

async function bootReport({ fetchImpl } = {}) {
  const env = createEnvironment();
  const ids = [
    "report-status",
    "report-enrichment-block",
    "report-short-summary",
    "report-overview",
    "report-keywords",
    "report-action-items",
    "report-term-corrections-block",
    "report-term-corrections",
    "report-term-corrections-note",
    "report-download-markdown",
  ];
  const nodes = {};
  ids.forEach((id) => {
    nodes[id] = env.mount(el(id === "report-download-markdown" ? "button" : "div", { id }));
  });
  nodes["report-status"].style.display = "none";
  nodes["report-enrichment-block"].style.display = "none";
  const calls = [];
  env.context.copilotAuthHeaders = () => ({ "X-Sales-Copilot-Token": "tok" });
  env.context.fetch = async (url, options) => {
    calls.push({ url, options });
    return fetchImpl ? fetchImpl(url, options) : { ok: true, json: async () => STORED };
  };
  env.load("dashboard/js/report.js");
  await settle();
  const socket = env.sockets.find((candidate) => candidate.url.endsWith("/ws/coaching"));
  assert.ok(socket, "report.js must subscribe to /ws/coaching");
  return { env, nodes, socket, calls };
}

const readyEvent = () => ({ type: "report_ready", session_id: "s-1", call_duration_ms: 4000 });

test("report_ready shows the status line, in Dutch", async () => {
  const { nodes, socket, calls } = await bootReport();

  socket.deliver(readyEvent());

  assert.equal(nodes["report-status"].style.display, "");
  assert.equal(nodes["report-status"].textContent, "Samenvatting en correcties worden gemaakt…");
  assert.equal(nodes["report-enrichment-block"].style.display, "none");
  assert.equal(calls.length, 0);
});

test("report_enriched reads the report with the token and shows it as text", async () => {
  const { env, nodes, socket, calls } = await bootReport();

  socket.deliver(readyEvent());
  socket.deliver(ENRICHED_EVENT);
  await settle();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/reports/s-1");
  assert.equal(calls[0].options.headers["X-Sales-Copilot-Token"], "tok");
  assert.equal(nodes["report-status"].style.display, "none");
  assert.equal(nodes["report-enrichment-block"].style.display, "");
  assert.equal(nodes["report-short-summary"].textContent, "Klant wil sneller offreren.");
  assert.equal(nodes["report-overview"].textContent, "Eerst de pijn, daarna de planning.");
  assert.equal(nodes["report-keywords"].textContent, "offertes, planning");
  const items = nodes["report-action-items"].querySelectorAll(".report-row");
  assert.deepEqual(items.map((item) => item.querySelector("strong").textContent), ["Offerte sturen", "Demo plannen"]);
  const corrections = nodes["report-term-corrections"].querySelectorAll(".term-correction-row");
  assert.equal(corrections.length, 1);
  assert.equal(corrections[0].querySelector(".term-correction-target").textContent, "Teamleader");
  assert.deepEqual(env.missingI18nKeys, []);
});

test("the session id is encoded in the endpoint path", async () => {
  const { socket, calls } = await bootReport();

  socket.deliver({ type: "report_ready", session_id: "a/../b", call_duration_ms: 1 });
  socket.deliver({ ...ENRICHED_EVENT, session_id: "a/../b" });
  await settle();

  assert.equal(calls[0].url, "/api/reports/a%2F..%2Fb");
});

test("a report that cannot be read leaves a neutral local-only line", async () => {
  const { nodes, socket } = await bootReport({ fetchImpl: async () => ({ ok: false, status: 404 }) });

  socket.deliver(readyEvent());
  socket.deliver(ENRICHED_EVENT);
  await settle();

  assert.equal(nodes["report-status"].style.display, "");
  assert.match(nodes["report-status"].textContent, /staat lokaal opgeslagen/);
  assert.equal(nodes["report-enrichment-block"].style.display, "none");
});

test("without report_enriched the line turns neutral after six minutes", async () => {
  const { env, nodes, socket } = await bootReport();

  socket.deliver(readyEvent());
  const timer = env.timers.timeouts.find((candidate) => candidate.delay === SIX_MINUTES_MS);
  assert.ok(timer, "a six-minute timer is set after report_ready");
  timer.fn();

  assert.equal(nodes["report-status"].style.display, "");
  assert.match(nodes["report-status"].textContent, /staat lokaal opgeslagen/);
});

test("the six-minute timer does nothing once the enriched report is shown", async () => {
  const { env, nodes, socket } = await bootReport();

  socket.deliver(readyEvent());
  socket.deliver(ENRICHED_EVENT);
  await settle();
  env.timers.timeouts.find((candidate) => candidate.delay === SIX_MINUTES_MS).fn();

  assert.equal(nodes["report-status"].style.display, "none");
});

test("a timer of an earlier report does not touch the status of a newer one", async () => {
  const { env, nodes, socket } = await bootReport();

  socket.deliver(readyEvent());
  const first = env.timers.timeouts.find((candidate) => candidate.delay === SIX_MINUTES_MS);
  env.context.resetReportView();
  socket.deliver(readyEvent());
  nodes["report-status"].textContent = "unchanged";
  first.fn();

  assert.equal(nodes["report-status"].textContent, "unchanged");
});

test("report_enriched for another session fetches nothing", async () => {
  const { socket, calls } = await bootReport();

  socket.deliver(readyEvent());
  socket.deliver({ ...ENRICHED_EVENT, session_id: "other" });
  await settle();

  assert.equal(calls.length, 0);
});

test("the Markdown export carries the enriched fields", async () => {
  const { env, nodes, socket } = await bootReport();
  socket.deliver(readyEvent());
  socket.deliver(ENRICHED_EVENT);
  await settle();

  const blobs = [];
  env.context.Blob = Blob;
  env.context.URL = { createObjectURL: (blob) => (blobs.push(blob), "blob:report"), revokeObjectURL: () => {} };
  nodes["report-download-markdown"].click();
  const markdown = await blobs[0].text();

  assert.match(markdown, /## Korte samenvatting\n\nKlant wil sneller offreren/);
  assert.match(markdown, /## Overzicht\n\nEerst de pijn/);
  assert.match(markdown, /## Trefwoorden\n\noffertes, planning/);
  assert.match(markdown, /## Actiepunten\n\n- Offerte sturen\n- Demo plannen/);
  assert.ok(markdown.includes("Origineel: Team Leader"), markdown);
});
