/**
 * The slice of dashboard/index.html the JS tests need, built programmatically.
 *
 * Ids, classes and data-attributes here must match the real shell. That is not
 * left to trust: tests/test_dashboard_js.py asserts every selector named below
 * also occurs in dashboard/index.html and demo/index.html, so this fixture
 * cannot drift away from the page it stands in for.
 */

import { el } from "./dom_stub.mjs";

/** data-module values on the real module checkboxes (dashboard/index.html). */
export const MODULE_KEYS = [
  "talk_time",
  "transcript",
  "pain_points",
  "presentation",
  "post_call_report",
];

/** Ids and classes this fixture reproduces, checked against the real HTML. */
export const FIXTURE_SELECTORS = [
  'id="preset-bar"',
  'id="screen-mode-toggle"',
  'id="detection-off-indicator"',
  'id="detector-status"',
  'id="detector-status-toggle"',
  'id="detector-status-label"',
  'id="detector-status-detail"',
  'class="module-toggle"',
  'class="toggle-button',
  'class="detector-status-label"',
  'class="detector-status-detail hidden"',
  'data-module="pain_points"',
  'data-module="presentation"',
  'data-mode="single"',
  'data-mode="dual"',
  'id="transcript-list"',
  'class="tr-body"',
  'id="client-select"',
  'id="client-cloud-sync-warning"',
];

/** The sidebar setup panel: preset bar, screen-mode toggle, module switches. */
export function buildSetupFixture(env, { checked = true } = {}) {
  const presetBar = el("div", { id: "preset-bar" });
  const screenToggle = el("div", {
    id: "screen-mode-toggle",
    className: "toggle-group",
    children: [
      el("button", { className: "toggle-button active", dataset: { mode: "single" } }),
      el("button", { className: "toggle-button", dataset: { mode: "dual" } }),
    ],
  });

  const moduleInputs = {};
  const moduleLabels = MODULE_KEYS.map((key) => {
    const input = el("input", { type: "checkbox", dataset: { module: key }, checked });
    moduleInputs[key] = input;
    return el("label", { className: "module-toggle", children: [input] });
  });

  const indicator = el("div", { id: "detection-off-indicator", className: "detection-off-indicator hidden" });
  const status = el("p", { className: "setup-status" });
  const clientSelect = el("select", { id: "client-select" });
  const cloudSyncWarning = el("p", { id: "client-cloud-sync-warning", className: "cloud-sync-warning hidden" });

  env.mount(presetBar);
  env.mount(screenToggle);
  moduleLabels.forEach((label) => env.mount(label));
  env.mount(indicator);
  env.mount(status);
  env.mount(clientSelect);
  env.mount(cloudSyncWarning);

  return { presetBar, screenToggle, moduleInputs, indicator, clientSelect, cloudSyncWarning };
}

/** The detector-status strip pinned under the pain-points panel. */
export function buildDetectorStatusFixture(env) {
  const label = el("span", { id: "detector-status-label", className: "detector-status-label" });
  const toggle = el("button", {
    id: "detector-status-toggle",
    className: "detector-status-toggle",
    children: [
      el("span", { className: "detector-status-dot" }),
      label,
      el("span", { className: "detector-status-caret" }),
    ],
  });
  const detail = el("div", { id: "detector-status-detail", className: "detector-status-detail hidden" });
  const host = el("div", {
    id: "detector-status",
    className: "detector-status is-idle",
    children: [toggle, detail],
  });
  env.mount(host);
  return { host, toggle, label, detail };
}

/** The live transcript panel dashboard/js/transcript.js binds to. */
export function buildTranscriptFixture(env) {
  const panel = el("div", { id: "transcript-list", className: "tr-body" });
  env.mount(panel);
  return { panel };
}

/** A detector_status payload with the shape status_publisher.py builds. */
export function detectorStatusPayload({
  state = "active",
  counts = {},
  uptimeMs = 10000,
  intervalMs = 5000,
  thresholds = { low: 0.5, high: 0.85 },
  policy = {
    only_classify_prospect: true,
    min_chunks_to_classify: 3,
    classification_debounce_seconds: 8.0,
  },
} = {}) {
  return {
    type: "detector_status",
    state,
    counts: {
      received: 0,
      skipped_not_prospect: 0,
      buffered_below_min: 0,
      debounced: 0,
      classified: 0,
      dropped_none: 0,
      dropped_low_confidence: 0,
      dispatched: 0,
      ...counts,
    },
    uptime_ms: uptimeMs,
    last_chunk_age_ms: null,
    interval_ms: intervalMs,
    thresholds,
    policy,
    timestamp_ms: 1700000000000,
  };
}
