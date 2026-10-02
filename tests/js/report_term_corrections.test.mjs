/**
 * dashboard/js/report.js -- term corrections on the post-call report (termenlijst-in-uitwerking D2).
 *
 * The transcript keeps the original; each correction shows the original next to the meant
 * term, built with textContent only. The list only comes with a full report: the
 * report_enriched event carries the counts, never the text. A report from before the field
 * renders exactly as it did. The Markdown export carries the corrections too.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createEnvironment, Element, el, settle } from "./dom_stub.mjs";

async function bootReport() {
  const env = createEnvironment({ routes: { "/api/reports/": {} } });
  const block = el("div", { id: "report-term-corrections-block" });
  block.style.display = "none";
  const list = el("div", { id: "report-term-corrections" });
  const note = el("p", { id: "report-term-corrections-note" });
  block.appendChild(list);
  block.appendChild(note);
  env.mount(block);
  const markdownButton = el("button", { id: "report-download-markdown" });
  env.mount(markdownButton);
  env.load("dashboard/js/report.js");
  await settle();
  const socket = env.sockets.find((candidate) => candidate.url.endsWith("/ws/coaching"));
  assert.ok(socket, "report.js must subscribe to /ws/coaching");
  return { env, block, list, note, socket, markdownButton };
}

const MARKUP = '<img src=x onerror="alert(1)">';

const fullReport = (extra = {}) => ({
  session_id: "s-1",
  call_duration_ms: 120000,
  full_transcript: [
    { speaker: "self", text: "We plannen alles in Team Leader.", start_ms: 0, end_ms: 2000 },
    { speaker: "prospect", text: `Dan vraag ik het aan ${MARKUP} van inkoop.`, start_ms: 65000, end_ms: 68000 },
  ],
  term_corrections: [
    { segment_index: 0, source: "Team Leader", occurrence: 1, target: "Teamleader", reason: "het planningspakket" },
    { segment_index: 1, source: MARKUP, occurrence: 1, target: "ROI", reason: "" },
  ],
  term_corrections_pii_limited: 0,
  ...extra,
});

/** Run `render` while recording every non-empty innerHTML write: text must never go through it. */
function recordMarkupWrites(render) {
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  const writes = [];
  Object.defineProperty(Element.prototype, "innerHTML", {
    ...descriptor,
    set(value) {
      if (value) {
        writes.push(String(value));
      }
      descriptor.set.call(this, value);
    },
  });
  try {
    render();
  } finally {
    Object.defineProperty(Element.prototype, "innerHTML", descriptor);
  }
  return writes;
}

test("a full report shows each original next to its correction, as text", async () => {
  const { env, block, list } = await bootReport();

  const markupWrites = recordMarkupWrites(() => env.context.renderReportView(fullReport()));

  assert.deepEqual(markupWrites, []);

  assert.equal(block.style.display, "");
  const rows = list.querySelectorAll(".term-correction-row");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector(".term-correction-original").textContent, "Team Leader");
  assert.equal(rows[0].querySelector(".term-correction-target").textContent, "Teamleader");
  assert.deepEqual(
    rows[0].querySelectorAll(".term-correction-label").map((label) => label.textContent),
    ["Origineel", "Correctie"],
  );
  assert.equal(rows[0].querySelector(".report-row-detail").textContent, "het planningspakket");
  // The original is text, never parsed markup: the element holds the raw string and no children.
  const original = rows[1].querySelector(".term-correction-original");
  assert.equal(original.textContent, MARKUP);
  assert.equal(original.children.length, 0);
  assert.equal(rows[1].querySelector(".report-row-detail"), null);
  assert.ok(rows[1].textContent.includes("01:05"), "the meta line names the segment's start time");
  assert.deepEqual(env.missingI18nKeys, []);
});

test("a report from before the field keeps the block hidden", async () => {
  const { block, list, socket } = await bootReport();

  socket.deliver({ type: "report_ready", session_id: "s-1", call_duration_ms: 4000 });

  assert.equal(block.style.display, "none");
  assert.equal(list.children.length, 0);
});

test("report_enriched shows the counts and no correction text", async () => {
  const { env, block, list, note, socket } = await bootReport();

  socket.deliver({ type: "report_ready", session_id: "s-1", call_duration_ms: 4000 });
  socket.deliver({
    type: "report_enriched",
    session_id: "s-1",
    gesprek_gevoerd: true,
    junk: false,
    junk_reason: null,
    term_correction_count: 3,
    term_corrections_pii_limited: 2,
  });

  assert.equal(block.style.display, "");
  assert.equal(list.children.length, 0);
  assert.match(note.textContent, /^3 termcorrecties staan in het opgeslagen rapport/);
  assert.match(note.textContent, /2 segmenten met kandidaattermen zijn voor het model geanonimiseerd/);
  assert.deepEqual(env.missingI18nKeys, []);
});

test("report_enriched without corrections leaves the block hidden", async () => {
  const { block, socket } = await bootReport();

  socket.deliver({ type: "report_ready", session_id: "s-1", call_duration_ms: 4000 });
  socket.deliver({
    type: "report_enriched",
    session_id: "s-1",
    gesprek_gevoerd: true,
    junk: false,
    junk_reason: null,
    term_correction_count: 0,
    term_corrections_pii_limited: 0,
  });

  assert.equal(block.style.display, "none");
});

async function exportMarkdown(env, markdownButton) {
  const blobs = [];
  env.context.Blob = Blob;
  env.context.URL = {
    createObjectURL: (blob) => {
      blobs.push(blob);
      return "blob:report";
    },
    revokeObjectURL: () => {},
  };
  markdownButton.click();
  assert.equal(blobs.length, 1);
  return blobs[0].text();
}

test("the Markdown export carries each original next to its correction", async () => {
  const { env, markdownButton } = await bootReport();
  env.context.renderReportView(fullReport({ term_corrections_pii_limited: 1 }));

  const markdown = await exportMarkdown(env, markdownButton);

  assert.match(markdown, /## Termcorrecties/);
  assert.ok(markdown.includes("- 00:00 - Origineel: Team Leader → Correctie: Teamleader"), markdown);
  assert.ok(markdown.includes("  - het planningspakket"), markdown);
  assert.ok(markdown.includes("1 segmenten met kandidaattermen"), markdown);
});

test("the Markdown export of a report without corrections has no term section", async () => {
  const { env, markdownButton } = await bootReport();
  env.context.renderReportView({ session_id: "s-1", call_duration_ms: 4000 });

  const markdown = await exportMarkdown(env, markdownButton);

  assert.ok(!markdown.includes("Termcorrecties"), markdown);
});
