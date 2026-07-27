(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getScriptTrackingWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/script-tracking`;
const panel = document.getElementById("script-tracking-panel");

// Phase 5 (Q2, design finding #5): fast-track matches are TENTATIVE
// ("Mogelijk geraakt", hollow marker) -- the fast matcher over-matches
// (measured precision 0.590, below the 0.80 advertise gate), so a fast-track
// hit must never render as a hard green check. Only the slow-track LLM
// confirm promotes a point to "Bevestigd" (solid check). "discussed" is the
// pre-Phase-5 legacy name for the same tentative state, kept so older
// payloads still render sensibly.
const STATUS_LABEL_KEYS = {
  missing: "scripttracking.status_missing",
  partial: "scripttracking.status_partial",
  tentative: "scripttracking.status_tentative",
  discussed: "scripttracking.status_tentative",
  confirmed: "scripttracking.status_confirmed",
};

const STATUS_ICONS = {
  missing: "○",
  partial: "◐",
  tentative: "◇",
  discussed: "◇",
  confirmed: "✓",
};

const formatPhase = (phase) => {
  if (!phase) {
    return "";
  }
  return phase.charAt(0).toUpperCase() + phase.slice(1);
};

const clearEmptyState = () => {
  if (!panel) {
    return;
  }
  const empty = panel.querySelector(".script-tracking-empty");
  if (empty) {
    panel.removeChild(empty);
  }
};

// Finding #4 (design doc section 7): a `degraded` flag now rides every
// script-tracking message (including the periodic `script_tracking_health`
// heartbeat) so the widget can tell when the live tick-off has gone stale --
// a failed WS publish or a failed confirmation cycle on the backend -- and
// say so, rather than silently keep showing the last-known state as current.
// No build step: plain DOM + inline style, matching this widget's existing
// vanilla approach. Passive, render-in-place, no popup/sound/modal -- the
// same breathing-bar principle the nudge banner already follows.
const HEALTH_PAUSED_CLASS = "script-tracking-paused";
const HEALTH_BANNER_STYLE = [
  "background: var(--border)",
  "color: var(--faint)",
  "border-radius: var(--r-sm, 8px)",
  "padding: 6px 10px",
  "font-size: 11px",
  "font-weight: 600",
  "margin-bottom: 8px",
].join("; ");

const setDegraded = (degraded) => {
  if (!panel) {
    return;
  }
  const existing = panel.querySelector(".script-tracking-health");
  if (degraded) {
    if (!existing) {
      const banner = document.createElement("div");
      banner.className = "script-tracking-health";
      banner.setAttribute("role", "status");
      banner.setAttribute("aria-live", "polite");
      banner.style.cssText = HEALTH_BANNER_STYLE;
      banner.textContent = window.t("scripttracking.health_paused");
      panel.prepend(banner);
    }
    panel.classList.add(HEALTH_PAUSED_CLASS);
  } else {
    if (existing) {
      panel.removeChild(existing);
    }
    panel.classList.remove(HEALTH_PAUSED_CLASS);
  }
};

const renderItem = (item) => {
  const status = item.status || "missing";
  const row = document.createElement("div");
  row.className = `script-tracking-item is-${status}`;
  row.dataset.pointId = item.point_id;

  const icon = document.createElement("span");
  icon.className = "script-tracking-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = STATUS_ICONS[status] || STATUS_ICONS.missing;

  const body = document.createElement("div");
  body.className = "script-tracking-body";

  const title = document.createElement("div");
  title.className = "script-tracking-title";
  title.textContent = item.title || item.point_id;

  const meta = document.createElement("div");
  meta.className = "script-tracking-meta";
  const statusLabelKey = STATUS_LABEL_KEYS[status];
  const statusLabel = statusLabelKey ? window.t(statusLabelKey) : status;
  meta.textContent = `${statusLabel}${item.phase ? ` · ${formatPhase(item.phase)}` : ""}`;

  body.appendChild(title);
  body.appendChild(meta);

  if (item.hint) {
    const hint = document.createElement("div");
    hint.className = "script-tracking-hint";
    hint.textContent = item.hint;
    body.appendChild(hint);
  }

  row.appendChild(icon);
  row.appendChild(body);
  return row;
};

const updatePanel = (payload) => {
  if (!panel || !payload) {
    return;
  }
  if (typeof payload.degraded === "boolean") {
    setDegraded(payload.degraded);
  }
  if (payload.type === "script_tracking_health") {
    // Heartbeat carries no rows of its own -- the `degraded` handling above
    // already applied it. Nothing else to render.
    return;
  }
  if (payload.type === "script_nudge") {
    const existing = panel.querySelector(".script-tracking-nudge");
    if (existing) {
      panel.removeChild(existing);
    }
    const nudge = document.createElement("div");
    nudge.className = "script-tracking-nudge";
    nudge.textContent = payload.top_hint || window.t("scripttracking.nudge_default");
    panel.prepend(nudge);
    return;
  }
  if (payload.type !== "script_coverage" || !Array.isArray(payload.coverage)) {
    return;
  }

  clearEmptyState();

  // Build a map of rendered rows keyed by point_id so we can update in place.
  const existingRows = new Map();
  panel.querySelectorAll(".script-tracking-item").forEach((row) => {
    existingRows.set(row.dataset.pointId, row);
  });

  payload.coverage.forEach((item) => {
    const pointId = item.point_id;
    const newRow = renderItem(item);
    if (existingRows.has(pointId)) {
      panel.replaceChild(newRow, existingRows.get(pointId));
      existingRows.delete(pointId);
    } else {
      panel.appendChild(newRow);
    }
  });

  // Remove any rows no longer present in the snapshot.
  existingRows.forEach((row) => {
    panel.removeChild(row);
  });
};

window.resetScriptTrackingPanel = () => {
  if (!panel) {
    return;
  }
  panel.classList.remove(HEALTH_PAUSED_CLASS);
  panel.innerHTML = `<div class="script-tracking-empty">${window.t("scripttracking.empty_state")}</div>`;
};

if (panel) {
  let socket;
  let retryCount = 0;

  const connect = () => {
    socket = new WebSocket(getScriptTrackingWsUrl());

    socket.addEventListener("message", (event) => {
      try {
        updatePanel(JSON.parse(event.data));
      } catch (error) {
        console.error("Invalid script-tracking message", error);
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
