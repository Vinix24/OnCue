/**
 * Track 3 (deep insight lane) dashboard panel: a compact, non-disruptive feed
 * of `/ws/insights` payloads (one line per insight, click to expand grounding)
 * plus the vraag-box that lets the seller ask the deep lane a free question.
 *
 * Wegklik reuses the existing hint-quality feedback flow (POST
 * /api/hint-feedback, feedback="down") so a dismissed insight is recorded the
 * same way a thumbs-down on a coaching hint is -- see core/feedback_store.py.
 *
 * Calm by design: no popups, no alerts. A failed POST is logged quietly.
 */
(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getInsightsWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/insights`;

const insightsPanel = document.getElementById("insights-panel");
const askForm = document.getElementById("insights-ask-form");
const askInput = document.getElementById("insights-ask-input");
const MAX_INSIGHTS = 30;
const INSIGHT_TYPES = ["doorvraag", "inzicht", "risico", "feitencheck", "antwoord"];

const sessionId = localStorage.getItem("tester-session-id") || Math.random().toString(36).slice(2, 10);
localStorage.setItem("tester-session-id", sessionId);

let budgetNoticeShown = false;

const formatTimestamp = (ms) => {
  if (typeof ms !== "number" || Number.isNaN(ms)) {
    return "--:--";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
};

const badgeLabel = (insightType) => {
  const key = INSIGHT_TYPES.includes(insightType) ? insightType : "inzicht";
  return window.t(`insights.type_${key}`);
};

const speculationLabel = (speculation) => {
  const key = speculation === "hoog" ? "hoog" : "laag";
  return window.t(`insights.speculation_${key}`);
};

const sourceLabel = (source) => {
  const key = source === "mcp" ? "mcp" : "engine";
  return window.t(`insights.source_${key}`);
};

const clearEmptyState = () => {
  if (!insightsPanel) {
    return;
  }
  const empty = insightsPanel.querySelector(".insights-empty");
  if (empty) {
    insightsPanel.removeChild(empty);
  }
};

const showEmptyState = () => {
  if (!insightsPanel) {
    return;
  }
  insightsPanel.innerHTML = `<div class="insights-empty">${window.t("insights.empty_state")}</div>`;
};

const pruneRows = () => {
  if (!insightsPanel) {
    return;
  }
  const rows = insightsPanel.querySelectorAll(".insight-row");
  if (rows.length <= MAX_INSIGHTS) {
    return;
  }
  for (let index = MAX_INSIGHTS; index < rows.length; index += 1) {
    insightsPanel.removeChild(rows[index]);
  }
};

const postJson = async (endpoint, body) => {
  await (window.copilotAuthReady || Promise.resolve());
  const headers = { "Content-Type": "application/json" };
  if (typeof window.copilotAuthHeaders === "function") {
    Object.assign(headers, window.copilotAuthHeaders());
  }
  const response = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`${endpoint} POST failed: HTTP ${response.status}`);
  }
  return response;
};

const dismissInsight = (row, payload) => {
  postJson("/api/hint-feedback", {
    hint: payload.text || "",
    feedback: "down",
    context_utterance: payload.grounding || "",
    timestamp_ms: payload.timestamp_ms,
    session_id: sessionId,
  }).catch((error) => {
    // Quiet failure: the vote is lost, the row still goes away.
    console.warn("insight dismiss vote not saved:", error.message);
  });
  row.remove();
  if (insightsPanel && !insightsPanel.querySelector(".insight-row")) {
    showEmptyState();
  }
};

const addInsightRow = (payload) => {
  if (!payload || payload.type !== "insight" || !insightsPanel) {
    return;
  }

  clearEmptyState();

  const row = document.createElement("div");
  row.className = "insight-row is-new is-collapsed";
  row.dataset.type = INSIGHT_TYPES.includes(payload.insight_type) ? payload.insight_type : "inzicht";
  row.dataset.source = payload.source === "mcp" ? "mcp" : "engine";
  if (payload.grounding) {
    row.title = payload.grounding;
  }

  const head = document.createElement("div");
  head.className = "insight-row-head";

  const badge = document.createElement("span");
  badge.className = "insight-badge";
  badge.textContent = badgeLabel(payload.insight_type);

  const speculation = document.createElement("span");
  speculation.className = `insight-speculation insight-speculation-${payload.speculation === "hoog" ? "hoog" : "laag"}`;
  speculation.textContent = speculationLabel(payload.speculation);

  const source = document.createElement("span");
  source.className = `insight-source insight-source-${row.dataset.source}`;
  source.textContent = sourceLabel(payload.source);

  const time = document.createElement("span");
  time.className = "insight-time";
  time.textContent = formatTimestamp(payload.timestamp_ms);

  head.appendChild(badge);
  head.appendChild(speculation);
  head.appendChild(source);
  head.appendChild(time);

  const textLine = document.createElement("div");
  textLine.className = "insight-row-text";
  textLine.textContent = payload.text || "";

  row.appendChild(head);
  row.appendChild(textLine);

  const detail = document.createElement("div");
  detail.className = "insight-row-detail";
  detail.hidden = true;

  if (typeof payload.question === "string" && payload.question.trim()) {
    const questionLine = document.createElement("div");
    questionLine.className = "insight-question";
    questionLine.textContent = `${window.t("insights.question_label")}: ${payload.question.trim()}`;
    detail.appendChild(questionLine);
  }

  if (payload.grounding) {
    const groundingLine = document.createElement("div");
    groundingLine.className = "insight-grounding";
    groundingLine.textContent = payload.grounding;
    detail.appendChild(groundingLine);
  }

  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "insight-dismiss-btn";
  dismissBtn.textContent = window.t("insights.dismiss_label");
  dismissBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    dismissInsight(row, payload);
  });
  detail.appendChild(dismissBtn);

  row.appendChild(detail);

  row.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    row.classList.toggle("is-collapsed", detail.hidden);
  });

  insightsPanel.prepend(row);
  pruneRows();

  setTimeout(() => {
    row.classList.remove("is-new");
  }, 600);
};

const showBudgetNotice = () => {
  if (budgetNoticeShown || !insightsPanel) {
    return;
  }
  budgetNoticeShown = true;
  clearEmptyState();
  const notice = document.createElement("div");
  notice.className = "insights-budget-notice";
  notice.textContent = window.t("insights.budget_exhausted");
  insightsPanel.prepend(notice);
};

window.resetInsightsPanel = () => {
  budgetNoticeShown = false;
  showEmptyState();
};

if (askForm && askInput) {
  askForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = askInput.value.trim();
    if (!text) {
      return;
    }
    askInput.disabled = true;
    postJson("/api/insights/ask", { text, ts: Date.now() })
      .catch((error) => {
        console.warn("insight ask not sent:", error.message);
      })
      .finally(() => {
        askInput.value = "";
        askInput.disabled = false;
        askInput.focus();
      });
  });
}

if (insightsPanel) {
  let socket;
  let retryCount = 0;

  const connect = () => {
    socket = new WebSocket(getInsightsWsUrl());

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload && payload.type === "insight") {
          addInsightRow(payload);
        } else if (payload && payload.type === "insight_budget_exhausted") {
          showBudgetNotice();
        }
      } catch (error) {
        console.error("Invalid insight message", error);
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
