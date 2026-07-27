(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getPainPointsWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/pain-points`;
const painPointsPanel = document.getElementById("pain-points-panel");
const MAX_PAIN_POINTS = 12;
const RESPONSE_LOCKED_TEASER = "\u{1F512} Pro: bekijk de beste tegenreactie";

const formatTimestamp = (ms) => {
  if (typeof ms !== "number" || Number.isNaN(ms)) {
    return "--:--";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
};

const formatCategory = (category) => {
  if (!category) {
    return window.t("common.category_unknown");
  }
  return category.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
};

const formatConfidence = (confidence) => {
  if (typeof confidence !== "number" || Number.isNaN(confidence)) {
    return "--";
  }
  const pct = confidence <= 1 ? confidence * 100 : confidence;
  return Math.round(pct);
};

const clearEmptyState = () => {
  if (!painPointsPanel) {
    return;
  }
  const empty = painPointsPanel.querySelector(".pain-points-empty");
  if (empty) {
    painPointsPanel.removeChild(empty);
  }
};

const collapseOlderCards = (latestCard) => {
  if (!painPointsPanel) {
    return;
  }
  Array.from(painPointsPanel.querySelectorAll(".pain-point-card")).forEach((card) => {
    const isLatest = card === latestCard;
    card.classList.toggle("is-expanded", isLatest);
    card.classList.toggle("is-collapsed", !isLatest);
  });
};

const pruneCards = () => {
  if (!painPointsPanel) {
    return;
  }
  const cards = painPointsPanel.querySelectorAll(".pain-point-card");
  if (cards.length <= MAX_PAIN_POINTS) {
    return;
  }
  for (let index = MAX_PAIN_POINTS; index < cards.length; index += 1) {
    painPointsPanel.removeChild(cards[index]);
  }
};

const buildCaseLabel = (payload) => {
  if (payload.case_matched) {
    return window.t("painpoints.case_match_label", { caseId: payload.case_id || "" }).trim();
  }
  return window.t("painpoints.no_case_match");
};

const addPainPointCard = (payload) => {
  if (!payload || payload.type !== "pain_point" || !painPointsPanel) {
    return;
  }

  clearEmptyState();

  const card = document.createElement("div");
  card.className = "pain-point-card is-expanded is-new";

  const header = document.createElement("div");
  header.className = "pain-point-header";

  const badge = document.createElement("span");
  badge.className = "pain-point-badge";
  badge.textContent = formatCategory(payload.category);

  const meta = document.createElement("div");
  meta.className = "pain-point-meta";

  const confidence = document.createElement("span");
  confidence.className = "pain-point-confidence";
  confidence.textContent = `${formatConfidence(payload.confidence)}%`;

  const timestamp = document.createElement("span");
  timestamp.textContent = formatTimestamp(payload.timestamp_ms);

  meta.appendChild(confidence);
  meta.appendChild(timestamp);
  header.appendChild(badge);
  header.appendChild(meta);

  const quote = document.createElement("div");
  quote.className = "pain-point-quote";
  quote.textContent = `"${payload.trigger_phrase || ""}"`;

  const caseLine = document.createElement("div");
  caseLine.className = `pain-point-case${payload.case_matched ? "" : " muted"}`;
  caseLine.textContent = buildCaseLabel(payload);

  card.appendChild(header);
  card.appendChild(quote);
  card.appendChild(caseLine);

  if (payload.response_suggestion) {
    const response = document.createElement("div");
    response.className = "pain-point-response";
    if (payload.response_suggestion === RESPONSE_LOCKED_TEASER) {
      response.classList.add("is-locked");
    }
    response.textContent = payload.response_suggestion;
    card.appendChild(response);

    if (window.createHintFeedbackControl) {
      card.appendChild(window.createHintFeedbackControl({
        hint: payload.response_suggestion,
        contextUtterance: payload.trigger_phrase || "",
        timestampMs: payload.timestamp_ms,
      }));
    }
  }

  painPointsPanel.prepend(card);
  collapseOlderCards(card);
  pruneCards();

  setTimeout(() => {
    card.classList.remove("is-new");
  }, 600);
};

window.resetPainPointsPanel = () => {
  if (!painPointsPanel) {
    return;
  }
  painPointsPanel.innerHTML = `<div class="pain-points-empty">${window.t("painpoints.empty_state")}</div>`;
};

if (painPointsPanel) {
  let socket;
  let retryCount = 0;

  const connect = () => {
    socket = new WebSocket(getPainPointsWsUrl());

    socket.addEventListener("message", (event) => {
      try {
        addPainPointCard(JSON.parse(event.data));
      } catch (error) {
        console.error("Invalid pain point message", error);
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
