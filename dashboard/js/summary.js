(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getSummaryWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/summary`;

const summaryToggle = document.getElementById("summary-toggle");
const summaryContent = document.getElementById("summary-content");
const summaryText = document.getElementById("summary-text");
const summaryMoments = document.getElementById("summary-moments");

if (!summaryToggle || !summaryContent || !summaryText || !summaryMoments) {
  return;
}

const formatTimestamp = (ms) => {
  if (typeof ms !== "number" || Number.isNaN(ms)) {
    return "--:--";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
};

const setCollapsed = (collapsed) => {
  summaryContent.hidden = collapsed;
  summaryContent.classList.toggle("is-collapsed", collapsed);
  summaryToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  const icon = summaryToggle.querySelector(".summary-toggle-icon");
  if (icon) {
    icon.textContent = collapsed ? "+" : "-";
  }
};

const renderMoments = (moments) => {
  summaryMoments.innerHTML = "";
  if (!Array.isArray(moments) || moments.length === 0) {
    const empty = document.createElement("li");
    empty.className = "summary-empty";
    empty.textContent = window.t("summary.empty_moments");
    summaryMoments.appendChild(empty);
    return;
  }

  moments.forEach((moment) => {
    if (!moment || typeof moment !== "object") {
      return;
    }
    const item = document.createElement("li");
    const description = typeof moment.description === "string" ? moment.description : window.t("summary.key_moment_fallback_label");

    const line = document.createElement("span");
    line.textContent = description;

    const time = document.createElement("span");
    time.className = "summary-moment-time";
    time.textContent = ` (${formatTimestamp(moment.timestamp_ms)})`;

    item.appendChild(line);
    item.appendChild(time);
    summaryMoments.appendChild(item);
  });
};

const renderSummary = (payload) => {
  if (!payload || payload.type !== "summary") {
    return;
  }
  if (typeof payload.text === "string" && payload.text.trim()) {
    summaryText.textContent = payload.text.trim();
  }
  renderMoments(payload.key_moments);
};

summaryToggle.addEventListener("click", () => {
  setCollapsed(summaryToggle.getAttribute("aria-expanded") === "true");
});
setCollapsed(true);

let socket;
let retryCount = 0;

const connect = () => {
  socket = new WebSocket(getSummaryWsUrl());

  socket.addEventListener("message", (event) => {
    try {
      renderSummary(JSON.parse(event.data));
    } catch (error) {
      console.error("Invalid summary message", error);
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

window.resetSummaryPanel = () => {
  summaryText.textContent = window.t("summary.empty_state");
  renderMoments([]);
  setCollapsed(true);
};
})();
