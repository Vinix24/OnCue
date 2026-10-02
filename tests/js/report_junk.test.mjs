/**
 * dashboard/js/report.js -- the junk marker on the post-call report.
 *
 * report_ready goes out before the post-call enrichment, so the junk decision arrives in a
 * second event (report_enriched). A report from before the junk filter carries no junk keys
 * and must render exactly as it did.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, el, settle } from "./dom_stub.mjs";

async function bootReport() {
  const env = createEnvironment({ routes: { "/api/reports/": {} } });
  const banner = el("div", { id: "report-junk-banner" });
  banner.style.display = "none";
  const reason = el("span", { id: "report-junk-reason" });
  env.mount(banner);
  env.mount(reason);
  env.load("dashboard/js/report.js");
  await settle();
  const socket = env.sockets.find((candidate) => candidate.url.endsWith("/ws/coaching"));
  assert.ok(socket, "report.js must subscribe to /ws/coaching");
  return { banner, reason, socket };
}

const readyEvent = (extra = {}) => ({ type: "report_ready", session_id: "s-1", call_duration_ms: 4000, ...extra });
const enrichedEvent = (extra = {}) => ({
  type: "report_enriched",
  session_id: "s-1",
  gesprek_gevoerd: false,
  junk: true,
  junk_reason: "model: geen gesprek (voicemail/IVR/geen gehoor); prospect-woorden: 3 (onder drempel 12)",
  ...extra,
});

test("a report_enriched with junk marks the report and shows the reason", async () => {
  const { banner, reason, socket } = await bootReport();

  socket.deliver(readyEvent());
  assert.equal(banner.style.display, "none");
  socket.deliver(enrichedEvent());

  assert.equal(banner.style.display, "");
  assert.match(reason.textContent, /prospect-woorden: 3/);
});

test("a report_enriched that is not junk leaves the banner hidden", async () => {
  const { banner, socket } = await bootReport();

  socket.deliver(readyEvent());
  socket.deliver(enrichedEvent({ gesprek_gevoerd: true, junk: false, junk_reason: null }));

  assert.equal(banner.style.display, "none");
});

test("a report_enriched for another session is ignored", async () => {
  const { banner, socket } = await bootReport();

  socket.deliver(readyEvent());
  socket.deliver(enrichedEvent({ session_id: "s-other" }));

  assert.equal(banner.style.display, "none");
});

test("a report without junk keys (from before the filter) renders as not junk", async () => {
  const { banner, socket } = await bootReport();

  socket.deliver(readyEvent());

  assert.equal(banner.style.display, "none");
});

test("a report that already carries junk keys shows the marker", async () => {
  const { banner, reason, socket } = await bootReport();

  socket.deliver(readyEvent({ junk: true, junk_reason: "model: geen gesprek" }));

  assert.equal(banner.style.display, "");
  assert.equal(reason.textContent, "model: geen gesprek");
});
