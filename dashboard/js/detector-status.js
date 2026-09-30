/*
 * Detector status strip — the middle case #203 and #206 left uncovered.
 *
 * #206 gave the seller a standing indicator for "AI detection is switched
 * off". #203 gave the log a first-chunk line, a bounded heartbeat and a
 * closing summary with per-reason counters. What was still invisible on
 * screen is the case in between: detection is ON, the call is running, and
 * nothing appears. Today's answer to "is it listening or is it broken?"
 * lived in a log file the seller does not open mid-call.
 *
 * This module subscribes to /ws/detector-status, where the detector now
 * publishes exactly those counters (counters and timestamps only — no
 * transcript text ever crosses this channel), and renders one quiet line:
 * alive, N utterances seen, M detections surfaced. The per-reason breakdown
 * is one click away, so "everything was your own speech" and "everything
 * scored under the threshold" are answerable without a terminal.
 *
 * It also owns the confidence thresholds the detector is actually running
 * with, and exposes window.detectorConfidenceTier() so the detection cards
 * can tier a match the same way PainPointRouter._tier_for_score() does
 * server-side (#205 made confidence proportional to real overlap, so an
 * `uncertain` match is now genuinely different from a `high` one).
 */
(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getDetectorStatusWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/detector-status`;

const host = document.getElementById("detector-status");
const toggle = document.getElementById("detector-status-toggle");
const label = document.getElementById("detector-status-label");
const detail = document.getElementById("detector-status-detail");

// Mirrors DetectorConfig's defaults (core/config.py). Used only until the
// first status payload arrives with the thresholds this run is really using.
const DEFAULT_THRESHOLDS = { low: 0.5, high: 0.85 };
const FALLBACK_INTERVAL_MS = 5000;
// The pulse is late once this many publish intervals have gone by in silence.
const STALE_INTERVALS = 3;
// Alive but nothing has reached the detector for this long during a call:
// that is not "no pain points yet", that is no audio getting through.
const NO_INPUT_GRACE_MS = 60000;
// Ordered as the consume loop filters: the first stage that ate the most
// chunks is the one worth naming.
const FILTER_REASONS = [
  ["skipped_not_prospect", "detector.reason_not_prospect"],
  ["buffered_below_min", "detector.reason_below_min"],
  ["debounced", "detector.reason_debounced"],
  ["dropped_low_confidence", "detector.reason_low_confidence"],
  ["dropped_none", "detector.reason_dropped_none"],
];
const COUNT_KEYS = [
  "received",
  "skipped_not_prospect",
  "buffered_below_min",
  "debounced",
  "classified",
  "dropped_none",
  "dropped_low_confidence",
  "dispatched",
];

const STATE_TONE = {
  waiting: "is-idle",
  listening: "is-live",
  active: "is-live",
  no_input: "is-warn",
  stale: "is-warn",
  stopped: "is-idle",
};

let lastStatus = null;
let lastStatusAt = null;
let thresholds = { ...DEFAULT_THRESHOLDS };
let expanded = false;

const now = () => Date.now();

const countOf = (status, key) => {
  const value = status?.counts?.[key];
  return typeof value === "number" && !Number.isNaN(value) ? value : 0;
};

const intervalMs = (status) => {
  const value = status?.interval_ms;
  return typeof value === "number" && value > 0 ? value : FALLBACK_INTERVAL_MS;
};

const isStale = (status) => {
  if (!status || lastStatusAt === null || status.state === "stopped") {
    return false;
  }
  return now() - lastStatusAt > intervalMs(status) * STALE_INTERVALS;
};

/** Resolve the display state — never trust the payload's own state alone,
 * because "we stopped hearing from the detector" is a state only the
 * dashboard can observe. */
const resolveState = (status) => {
  if (!status) {
    return "waiting";
  }
  if (isStale(status)) {
    return "stale";
  }
  if (status.state === "stopped") {
    return "stopped";
  }
  if (countOf(status, "received") > 0) {
    return "active";
  }
  const uptime = typeof status.uptime_ms === "number" ? status.uptime_ms : 0;
  return uptime > NO_INPUT_GRACE_MS ? "no_input" : "listening";
};

const summaryFor = (state, status) => {
  if (state === "active") {
    return window.t("detector.status_active", {
      received: countOf(status, "received"),
      detections: countOf(status, "dispatched"),
    });
  }
  return window.t(`detector.status_${state}`);
};

/** The single sentence that answers "why is nothing showing up?". */
const reasonFor = (state, status) => {
  if (state === "waiting") {
    return window.t("detector.reason_waiting");
  }
  if (state === "stale") {
    return window.t("detector.reason_stale");
  }
  if (state === "no_input") {
    return window.t("detector.reason_no_input");
  }
  if (countOf(status, "dispatched") > 0) {
    return window.t("detector.reason_healthy", { detections: countOf(status, "dispatched") });
  }
  if (countOf(status, "received") === 0) {
    return window.t("detector.reason_nothing_received");
  }
  let bestKey = null;
  let bestCount = 0;
  FILTER_REASONS.forEach(([counter, messageKey]) => {
    const value = countOf(status, counter);
    if (value > bestCount) {
      bestCount = value;
      bestKey = messageKey;
    }
  });
  if (bestKey === "detector.reason_below_min") {
    return window.t(bestKey, { min: status?.policy?.min_chunks_to_classify ?? "?" });
  }
  if (bestKey) {
    return window.t(bestKey, { count: bestCount });
  }
  return window.t("detector.reason_nothing_matched");
};

const renderDetail = (state, status) => {
  if (!detail) {
    return;
  }
  const nodes = [];

  const reason = document.createElement("p");
  reason.className = "detector-status-reason";
  reason.textContent = reasonFor(state, status);
  nodes.push(reason);

  if (status) {
    const table = document.createElement("dl");
    table.className = "detector-status-counts";
    COUNT_KEYS.forEach((key) => {
      const term = document.createElement("dt");
      term.textContent = window.t(`detector.count_${key}`);
      const value = document.createElement("dd");
      value.textContent = String(countOf(status, key));
      table.appendChild(term);
      table.appendChild(value);
    });
    nodes.push(table);
  }

  detail.replaceChildren(...nodes);
};

const render = () => {
  if (!host) {
    return;
  }
  const state = resolveState(lastStatus);
  Object.values(STATE_TONE).forEach((tone) => host.classList.remove(tone));
  host.classList.add(STATE_TONE[state] || "is-idle");
  host.dataset.state = state;
  if (label) {
    label.textContent = summaryFor(state, lastStatus);
  }
  renderDetail(state, lastStatus);
};

const setExpanded = (next) => {
  expanded = next;
  if (toggle) {
    toggle.setAttribute("aria-expanded", String(expanded));
  }
  if (detail) {
    detail.classList.toggle("hidden", !expanded);
  }
};

const handleStatus = (payload) => {
  if (!payload || payload.type !== "detector_status") {
    return;
  }
  lastStatus = payload;
  lastStatusAt = now();
  const low = payload?.thresholds?.low;
  const high = payload?.thresholds?.high;
  thresholds = {
    low: typeof low === "number" ? low : DEFAULT_THRESHOLDS.low,
    high: typeof high === "number" ? high : DEFAULT_THRESHOLDS.high,
  };
  render();
};

/**
 * Tier a detection's confidence against the thresholds the detector is
 * running with — the same rule as PainPointRouter._tier_for_score(). Before
 * #205 every keyword match came back at a hardcoded 0.95, so this would have
 * been a constant; it is now a real signal.
 */
window.detectorConfidenceTier = (confidence) => {
  if (typeof confidence !== "number" || Number.isNaN(confidence)) {
    return "unknown";
  }
  const value = confidence > 1 ? confidence / 100 : confidence;
  return value >= thresholds.high ? "high" : "uncertain";
};

window.resetDetectorStatus = () => {
  lastStatus = null;
  lastStatusAt = null;
  setExpanded(false);
  render();
};

if (host) {
  if (toggle) {
    toggle.addEventListener("click", () => setExpanded(!expanded));
  }
  setExpanded(false);
  window.SalesCopilotI18n.ready.then(render);

  // The staleness check is a clock, not a message: a detector that dies mid
  // call stops publishing, and silence is precisely the signal to surface.
  setInterval(render, FALLBACK_INTERVAL_MS);

  let socket;
  let retryCount = 0;

  const connect = () => {
    socket = new WebSocket(getDetectorStatusWsUrl());

    socket.addEventListener("message", (event) => {
      try {
        handleStatus(JSON.parse(event.data));
      } catch (error) {
        console.error("Invalid detector status message", error);
      }
    });

    socket.addEventListener("close", () => {
      const delay = Math.min(1000 * 2 ** retryCount, 30000);
      retryCount += 1;
      setTimeout(connect, delay);
    });

    socket.addEventListener("open", () => {
      retryCount = 0;
    });

    socket.addEventListener("error", () => {
      socket.close();
    });
  };

  connect();
}
})();
