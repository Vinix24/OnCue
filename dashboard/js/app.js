const WS_BASE_URL = window.WS_BASE_URL || (typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760");
const WS_BASE = `${WS_BASE_URL}/ws`;

const selectors = {
  bar: document.getElementById("talk-time-bar"),
  rollingSelf: document.getElementById("rolling-self"),
  rollingProspect: document.getElementById("rolling-prospect"),
  cumulativeSelf: document.getElementById("cumulative-self"),
  cumulativeProspect: document.getElementById("cumulative-prospect"),
  callDuration: document.getElementById("call-duration"),
  monologueWarning: document.getElementById("monologue-warning"),
  systemStatus: document.getElementById("system-status"),
  phaseButtons: Array.from(document.querySelectorAll(".phase-button")),
  connectionDot: document.getElementById("connection-dot"),
  connectionText: document.getElementById("connection-text"),
  themeToggle: document.getElementById("theme-toggle"),
  swapSpeakers: document.getElementById("swap-speakers"),
};

let openConnections = 0;
let reconnectingConnections = 0;
let lastCallDurationMs = 0;
let lastCallDurationUpdate = null;
let callDurationTimer = null;
let currentPhase = "discovery";
let currentPhaseSource = "manual";
let manualPhaseOverride = false;
let systemStatusHideTimer = null;
const phaseLabels = new Map(
  selectors.phaseButtons.map((button) => [button.dataset.phase, button.textContent.trim()]),
);

const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

const toPercent = (value) => {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return 0;
  }
  const normalized = value <= 1 ? value * 100 : value;
  return clamp(Math.round(normalized), 0, 100);
};

const formatDuration = (ms) => {
  if (typeof ms !== "number" || Number.isNaN(ms)) {
    return "00:00";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const two = (num) => String(num).padStart(2, "0");
  return hours > 0 ? `${two(hours)}:${two(minutes)}:${two(seconds)}` : `${two(minutes)}:${two(seconds)}`;
};

const updateCallDurationDisplay = () => {
  if (!selectors.callDuration || lastCallDurationUpdate === null) {
    return;
  }
  const elapsed = Date.now() - lastCallDurationUpdate;
  const displayMs = Math.max(0, lastCallDurationMs + elapsed);
  selectors.callDuration.textContent = formatDuration(displayMs);
};

const startCallDurationTimer = () => {
  if (callDurationTimer) {
    return;
  }
  callDurationTimer = setInterval(updateCallDurationDisplay, 1000);
};

const setConnectionStatus = (status) => {
  if (!selectors.connectionDot || !selectors.connectionText) {
    return;
  }
  selectors.connectionDot.classList.remove("disconnected", "reconnecting");
  if (status === "connected") {
    selectors.connectionText.textContent = window.t("header.status_connected");
    return;
  }
  if (status === "reconnecting") {
    selectors.connectionDot.classList.add("reconnecting");
    selectors.connectionText.textContent = window.t("header.status_reconnecting");
    return;
  }
  selectors.connectionDot.classList.add("disconnected");
  selectors.connectionText.textContent = window.t("header.status_disconnected");
};

const updateConnectionCount = (delta) => {
  openConnections = Math.max(0, openConnections + delta);
  if (openConnections > 0) {
    setConnectionStatus("connected");
  } else if (reconnectingConnections > 0) {
    setConnectionStatus("reconnecting");
  } else {
    setConnectionStatus("disconnected");
  }
};

const updateReconnectingCount = (delta) => {
  reconnectingConnections = Math.max(0, reconnectingConnections + delta);
  if (openConnections > 0) {
    setConnectionStatus("connected");
  } else if (reconnectingConnections > 0) {
    setConnectionStatus("reconnecting");
  } else {
    setConnectionStatus("disconnected");
  }
};

const showWarning = (message, severity = "amber") => {
  if (!selectors.monologueWarning) {
    return;
  }
  selectors.monologueWarning.textContent = message;
  selectors.monologueWarning.style.color = severity === "red" ? "var(--danger)" : "var(--warn)";
  selectors.monologueWarning.classList.add("visible");
  window._heroSpotlightOwnedByObjection = false;
  window.hintFeedback?.showHero({ hint: message });
  if (selectors.monologueWarning.hideTimer) {
    clearTimeout(selectors.monologueWarning.hideTimer);
  }
  selectors.monologueWarning.hideTimer = setTimeout(() => {
    selectors.monologueWarning.classList.remove("visible");
    selectors.monologueWarning.style.color = "";
    selectors.monologueWarning.textContent = window.t("coaching.monologue_default");
    window.hintFeedback?.hideHero();
  }, 5000);
};

const clearSystemStatusTimer = () => {
  if (systemStatusHideTimer) {
    clearTimeout(systemStatusHideTimer);
    systemStatusHideTimer = null;
  }
};

const hideSystemStatus = () => {
  if (!selectors.systemStatus) {
    return;
  }
  clearSystemStatusTimer();
  selectors.systemStatus.classList.remove("warming", "is-fading");
  selectors.systemStatus.classList.add("hidden");
  selectors.systemStatus.textContent = "";
};

const showSystemStatusWarmup = (message) => {
  if (!selectors.systemStatus) {
    return;
  }
  clearSystemStatusTimer();
  selectors.systemStatus.classList.remove("hidden", "is-fading");
  selectors.systemStatus.classList.add("warming");

  const spinner = document.createElement("span");
  spinner.className = "system-status-spinner";
  spinner.setAttribute("aria-hidden", "true");

  const text = document.createElement("span");
  text.textContent = message || window.t("header.system_status_warming_default");

  selectors.systemStatus.replaceChildren(spinner, text);
};

const fadeOutSystemStatusReady = () => {
  if (!selectors.systemStatus) {
    return;
  }
  clearSystemStatusTimer();
  selectors.systemStatus.classList.remove("warming");
  selectors.systemStatus.classList.remove("hidden");
  selectors.systemStatus.classList.add("is-fading");
  systemStatusHideTimer = setTimeout(() => {
    hideSystemStatus();
  }, 2000);
};

const setActivePhase = (phase, source = "manual") => {
  if (!phase) {
    return;
  }
  currentPhase = phase;
  currentPhaseSource = source;
  selectors.phaseButtons.forEach((button) => {
    const buttonPhase = button.dataset.phase;
    const isActive = buttonPhase === phase;
    button.classList.toggle("active", isActive);
    const baseLabel = phaseLabels.get(buttonPhase) || button.textContent;
    if (isActive && source === "auto") {
      button.textContent = `${baseLabel} (${window.t("header.phase_auto_indicator")})`;
      return;
    }
    button.textContent = baseLabel;
  });
};

const updateTalkTime = (payload) => {
  if (!payload || !selectors.bar) {
    return;
  }
  const rollingSelf = payload.rolling_self_pct ?? payload.rollingSelfPct ?? payload.rolling?.self_pct;
  const rollingProspect = payload.rolling_prospect_pct ?? payload.rollingProspectPct ?? payload.rolling?.prospect_pct;
  const cumulativeSelf = payload.cumulative_self_pct ?? payload.cumulativeSelfPct ?? payload.cumulative?.self_pct;
  const cumulativeProspect = payload.cumulative_prospect_pct ?? payload.cumulativeProspectPct ?? payload.cumulative?.prospect_pct;
  const status = payload.status || "green";
  const callDurationMs = payload.call_duration_ms ?? payload.callDurationMs ?? 0;
  const phase = payload.phase;

  const rollingSelfPct = toPercent(rollingSelf);
  const rollingProspectPct = toPercent(rollingProspect);
  const cumulativeSelfPct = toPercent(cumulativeSelf);
  const cumulativeProspectPct = toPercent(cumulativeProspect);

  selectors.bar.style.setProperty("--self-percent", `${rollingSelfPct}%`);
  selectors.bar.classList.remove("status-green", "status-amber", "status-red");
  selectors.bar.classList.add(`status-${status}`);

  if (selectors.rollingSelf) {
    selectors.rollingSelf.textContent = `${rollingSelfPct}%`;
  }
  if (selectors.rollingProspect) {
    selectors.rollingProspect.textContent = `${rollingProspectPct}%`;
  }
  if (selectors.cumulativeSelf) {
    selectors.cumulativeSelf.textContent = `${cumulativeSelfPct}%`;
  }
  if (selectors.cumulativeProspect) {
    selectors.cumulativeProspect.textContent = `${cumulativeProspectPct}%`;
  }
  if (typeof callDurationMs === "number" && !Number.isNaN(callDurationMs)) {
    lastCallDurationMs = callDurationMs;
    lastCallDurationUpdate = Date.now();
    updateCallDurationDisplay();
    startCallDurationTimer();
  }

  if (phase) {
    setActivePhase(phase, currentPhaseSource);
  }
};

const handleCoachingMessage = (payload) => {
  if (!payload) {
    return;
  }
  if (payload.type === "license_grace" || payload.type === "license_expired") {
    window.dispatchEvent(new CustomEvent("license-grace-event", { detail: { event: payload.type } }));
    return;
  }
  if (payload.type === "system_status") {
    if (payload.state === "warming_up") {
      showSystemStatusWarmup(payload.message);
      return;
    }
    if (payload.state === "ready") {
      fadeOutSystemStatusReady();
      return;
    }
    return;
  }
  if (payload.type === "audio_warning") {
    const stream = payload.stream === "self" ? "self" : "prospect";
    const labelKey = stream === "self" ? "header.audio_warning_self_label" : "header.audio_warning_prospect_label";
    const msg = payload.message || window.t("header.audio_warning_message", { label: window.t(labelKey) });
    showAudioWarning(stream, msg);
    return;
  }
  if (payload.alert_type !== "monologue_warning") {
    return;
  }
  const message = payload.message || window.t("coaching.time_to_listen_default");
  const severity = payload.severity || "amber";
  showWarning(message, severity);
};

const createReconnectingSocket = (path, { onMessage }) => {
  let socket;
  let retryCount = 0;
  let isOpen = false;
  let isReconnecting = false;
  let shouldReconnect = true;

  const connect = () => {
    if (!shouldReconnect) {
      return;
    }
    socket = new WebSocket(`${WS_BASE}/${path}`);

    socket.addEventListener("open", () => {
      retryCount = 0;
      if (isReconnecting) {
        isReconnecting = false;
        updateReconnectingCount(-1);
      }
      if (!isOpen) {
        isOpen = true;
        updateConnectionCount(1);
      }
    });

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        onMessage(payload);
      } catch (error) {
        console.error("Invalid message", error);
      }
    });

    socket.addEventListener("close", () => {
      if (isOpen) {
        isOpen = false;
        updateConnectionCount(-1);
      }
      if (!shouldReconnect) {
        return;
      }
      if (!isReconnecting) {
        isReconnecting = true;
        updateReconnectingCount(1);
      }
      const delay = Math.min(1000 * 2 ** retryCount, 30000);
      retryCount += 1;
      setTimeout(connect, delay);
    });

    socket.addEventListener("error", () => {
      socket.close();
    });
  };

  connect();

  return {
    send: (data) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(data));
      }
    },
    close: () => {
      shouldReconnect = false;
      if (isReconnecting) {
        isReconnecting = false;
        updateReconnectingCount(-1);
      }
      if (isOpen) {
        isOpen = false;
        updateConnectionCount(-1);
      }
      if (socket) {
        socket.close();
      }
    },
  };
};

const postHubAction = async (path, payload = null) => {
  await window.copilotAuthReady;
  const options = {
    method: "POST",
    headers: { ...window.copilotAuthHeaders() },
  };
  if (payload) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    throw new Error(`Hub action failed: ${path}`);
  }
};

const applyTheme = (theme) => {
  document.body.classList.remove("theme-dark", "theme-light");
  document.body.classList.add(theme === "dark" ? "theme-dark" : "theme-light");
  if (selectors.themeToggle) {
    selectors.themeToggle.setAttribute("aria-pressed", String(theme === "dark"));
  }
};

const applyThemeToggleLabel = (theme) => {
  if (!selectors.themeToggle) {
    return;
  }
  const isDark = theme === "dark";
  selectors.themeToggle.textContent = isDark
    ? window.t("header.theme_toggle_to_light")
    : window.t("header.theme_toggle_to_dark");
};

const initThemeToggle = () => {
  if (!selectors.themeToggle) {
    return;
  }
  const storedTheme = localStorage.getItem("dashboard-theme");
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const initialTheme = storedTheme || (prefersDark ? "dark" : "light");
  applyTheme(initialTheme);
  window.SalesCopilotI18n.ready.then(() => applyThemeToggleLabel(initialTheme));
  selectors.themeToggle.addEventListener("click", () => {
    const isDark = document.body.classList.contains("theme-dark");
    const nextTheme = isDark ? "light" : "dark";
    applyTheme(nextTheme);
    applyThemeToggleLabel(nextTheme);
    localStorage.setItem("dashboard-theme", nextTheme);
  });
};

let talkSocket;
let coachingSocket;
let phaseSocket;
let configSocket;
let socketsStarted = false;

selectors.phaseButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const phase = button.dataset.phase;
    if (phase) {
      manualPhaseOverride = true;
      setActivePhase(phase, "manual");
      postHubAction("/api/phase", { phase }).catch(console.error);
    }
  });
});

if (selectors.swapSpeakers) {
  selectors.swapSpeakers.addEventListener("click", () => {
    postHubAction("/api/swap-speakers").catch(console.error);
    if (window.swapTranscriptSpeakers) {
      window.swapTranscriptSpeakers();
    }
  });
}

const _audioWarningElements = {
  self: document.getElementById("audio-warning-self"),
  prospect: document.getElementById("audio-warning-prospect"),
};
const _audioWarningTimers = { self: null, prospect: null };

const clearAudioWarning = (stream) => {
  const el = _audioWarningElements[stream];
  if (!el) {
    return;
  }
  if (_audioWarningTimers[stream]) {
    clearTimeout(_audioWarningTimers[stream]);
    _audioWarningTimers[stream] = null;
  }
  el.classList.add("hidden");
};

const clearAudioWarnings = () => {
  clearAudioWarning("self");
  clearAudioWarning("prospect");
};

const showAudioWarning = (stream, message) => {
  const el = _audioWarningElements[stream];
  if (!el) {
    return;
  }
  el.textContent = `⚠ ${message}`;
  el.classList.remove("hidden");
  if (_audioWarningTimers[stream]) {
    clearTimeout(_audioWarningTimers[stream]);
  }
  _audioWarningTimers[stream] = setTimeout(() => clearAudioWarning(stream), 30000);
};

const resetCallState = () => {
  lastCallDurationMs = 0;
  lastCallDurationUpdate = null;
  manualPhaseOverride = false;
  currentPhase = "discovery";
  currentPhaseSource = "manual";
  if (callDurationTimer) {
    clearInterval(callDurationTimer);
    callDurationTimer = null;
  }
  if (selectors.callDuration) {
    selectors.callDuration.textContent = "00:00";
  }
  if (selectors.bar) {
    selectors.bar.style.setProperty("--self-percent", "50%");
    selectors.bar.classList.remove("status-amber", "status-red");
    selectors.bar.classList.add("status-green");
  }
  if (selectors.rollingSelf) {
    selectors.rollingSelf.textContent = "--";
  }
  if (selectors.rollingProspect) {
    selectors.rollingProspect.textContent = "--";
  }
  if (selectors.cumulativeSelf) {
    selectors.cumulativeSelf.textContent = "--";
  }
  if (selectors.cumulativeProspect) {
    selectors.cumulativeProspect.textContent = "--";
  }
  hideSystemStatus();
  setActivePhase("discovery", "manual");
};

const resetCallPanels = () => {
  resetCallState();
  if (window.resetTranscriptPanel) {
    window.resetTranscriptPanel();
  }
  if (window.resetPainPointsPanel) {
    window.resetPainPointsPanel();
  }
  if (window.resetObjectionsPanel) {
    window.resetObjectionsPanel();
  }
  if (window.resetOpportunitiesPanel) {
    window.resetOpportunitiesPanel();
  }
  if (window.resetSuggestionsPanel) {
    window.resetSuggestionsPanel();
  }
  if (window.resetSummaryPanel) {
    window.resetSummaryPanel();
  }
  if (window.resetScriptTrackingPanel) {
    window.resetScriptTrackingPanel();
  }
  clearAudioWarnings();
};
window.resetCallPanels = resetCallPanels;

const handlePhaseMessage = (payload) => {
  if (!payload || payload.type !== "phase_change") {
    return;
  }
  const phase = payload.phase;
  const source = payload.source === "auto" ? "auto" : "manual";
  if (typeof phase !== "string") {
    return;
  }
  if (source === "auto" && manualPhaseOverride) {
    return;
  }
  if (source !== "auto") {
    manualPhaseOverride = true;
  }
  setActivePhase(phase, source);
};

const handleConfigMessage = (payload) => {
  if (!payload) {
    return;
  }
  if (payload.type === "consent_nudge") {
    showWarning(payload.message || window.t("consent.not_recorded_warning"), "red");
    return;
  }
  if (payload.type === "call_blocked" && payload.reason === "consent_required") {
    window.alert(window.t("consent.call_blocked_alert"));
    stopCallSockets();
    if (window.showSetupView) {
      window.showSetupView();
    }
    if (window._resetStartCallButton) {
      window._resetStartCallButton();
    }
    return;
  }
  if (payload.type === "call_ended") {
    stopCallSockets();
    if (window.showSetupView) {
      window.showSetupView();
    }
  }
};

const startCallSockets = () => {
  if (socketsStarted) {
    return;
  }
  resetCallPanels();
  socketsStarted = true;
  talkSocket = createReconnectingSocket("talk-time", {
    onMessage: updateTalkTime,
  });
  coachingSocket = createReconnectingSocket("coaching", {
    onMessage: handleCoachingMessage,
  });
  phaseSocket = createReconnectingSocket("phase", {
    onMessage: handlePhaseMessage,
  });
  configSocket = createReconnectingSocket("config", {
    onMessage: handleConfigMessage,
  });
};

const stopCallSockets = () => {
  if (!socketsStarted) {
    return;
  }
  socketsStarted = false;
  talkSocket?.close();
  coachingSocket?.close();
  phaseSocket?.close();
  configSocket?.close();
  talkSocket = null;
  coachingSocket = null;
  phaseSocket = null;
  configSocket = null;
  clearAudioWarnings();
};

window.startCallSockets = startCallSockets;
window.stopCallSockets = stopCallSockets;

window.SalesCopilotI18n.ready.then(() => setConnectionStatus("disconnected"));
initThemeToggle();
