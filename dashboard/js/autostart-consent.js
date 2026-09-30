/**
 * Autostart consent prompt: subscribes to /ws/system, shows a non-modal
 * prompt when a meeting app or phone call is detected, and lets the user
 * confirm or decline runtime auto-arm.
 *
 * Detection alone never starts a session — the explicit per-call consent
 * tick (POST /api/autostart/confirm) is the only path to arm. This is a
 * hard product rule (see core/autostart_monitor.py). The prompt also
 * listens on /ws/config for start_call so a call started manually (or by
 * another consent source) cleans up any stale prompt.
 */
(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getSystemWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/system`;
const getConfigWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/config`;

const promptEl = document.getElementById("autostart-consent-prompt");
const processNameEl = document.getElementById("autostart-process-name");
const confirmBtn = document.getElementById("autostart-confirm-btn");
const declineBtn = document.getElementById("autostart-decline-btn");
const outcomeEl = document.getElementById("autostart-outcome");

if (!promptEl) {
  return;
}

let pendingProcess = null;

const humanizeProcess = (process) => {
  if (!process) {
    return "";
  }
  // avconferenced is the macOS telephony daemon — show a human label
  if (process === "avconferenced") {
    return window.t("autostart.process_phone_call");
  }
  return process;
};

const showPrompt = (processName) => {
  pendingProcess = processName;
  if (processNameEl) {
    processNameEl.textContent = humanizeProcess(processName);
  }
  promptEl.classList.remove("hidden");
  if (outcomeEl) {
    outcomeEl.classList.add("hidden");
    outcomeEl.textContent = "";
  }
  if (confirmBtn) {
    confirmBtn.disabled = false;
  }
  if (declineBtn) {
    declineBtn.disabled = false;
  }
};

const hidePrompt = () => {
  promptEl.classList.add("hidden");
  pendingProcess = null;
};

const showOutcome = (key) => {
  if (!outcomeEl) {
    return;
  }
  outcomeEl.textContent = window.t(key);
  outcomeEl.classList.remove("hidden");
  // Auto-dismiss the full prompt after a short read window
  setTimeout(() => {
    hidePrompt();
  }, 4000);
};

const postAutostart = async (endpoint) => {
  await (window.copilotAuthReady || Promise.resolve());
  const headers = { "Content-Type": "application/json" };
  if (typeof window.copilotAuthHeaders === "function") {
    Object.assign(headers, window.copilotAuthHeaders());
  }
  const response = await fetch(endpoint, {
    method: "POST",
    headers,
  });
  if (!response.ok) {
    throw new Error(`${endpoint} POST failed: HTTP ${response.status}`);
  }
  return response;
};

if (confirmBtn) {
  confirmBtn.addEventListener("click", async () => {
    if (confirmBtn.disabled) {
      return;
    }
    confirmBtn.disabled = true;
    if (declineBtn) {
      declineBtn.disabled = true;
    }
    try {
      await postAutostart("/api/autostart/confirm");
      showOutcome("autostart.confirmed");
    } catch (error) {
      if (error.message.includes("409")) {
        // Normal race: the prompt expired or was already handled before
        // the user clicked. Clean up silently.
        hidePrompt();
      } else {
        console.warn("autostart confirm failed:", error.message);
        confirmBtn.disabled = false;
        if (declineBtn) {
          declineBtn.disabled = false;
        }
      }
    }
  });
}

if (declineBtn) {
  declineBtn.addEventListener("click", async () => {
    if (declineBtn.disabled) {
      return;
    }
    confirmBtn.disabled = true;
    declineBtn.disabled = true;
    try {
      await postAutostart("/api/autostart/decline");
      showOutcome("autostart.declined");
    } catch (error) {
      // Decline is best-effort; even if the POST fails (e.g. hub down),
      // hide the prompt so the user isn't stuck.
      console.warn("autostart decline failed:", error.message);
      hidePrompt();
    }
  });
}

/* ---- /ws/system subscription — autostart_consent_required messages ---- */

let systemRetryCount = 0;

const connectSystem = () => {
  const socket = new WebSocket(getSystemWsUrl());

  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload && payload.type === "autostart_consent_required") {
        showPrompt(payload.process || "");
      }
    } catch (error) {
      console.error("Invalid system message", error);
    }
  });

  socket.addEventListener("close", () => {
    const delay = Math.min(1000 * 2 ** systemRetryCount, 30000);
    systemRetryCount += 1;
    setTimeout(connectSystem, delay);
  });

  socket.addEventListener("open", () => {
    systemRetryCount = 0;
  });

  socket.addEventListener("error", () => {
    socket.close();
  });
};

connectSystem();

/* ---- /ws/config subscription — zombie-prompt prevention ---- */

let configRetryCount = 0;

const connectConfig = () => {
  const socket = new WebSocket(getConfigWsUrl());

  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload && (payload.type === "start_call" || payload.type === "call_started")) {
        // A call started (manually or via auto-arm) — hide any stale prompt
        hidePrompt();
      }
    } catch (_error) {
      // Ignore parse errors on config channel
    }
  });

  socket.addEventListener("close", () => {
    const delay = Math.min(1000 * 2 ** configRetryCount, 30000);
    configRetryCount += 1;
    setTimeout(connectConfig, delay);
  });

  socket.addEventListener("open", () => {
    configRetryCount = 0;
  });

  socket.addEventListener("error", () => {
    socket.close();
  });
};

connectConfig();
})();
