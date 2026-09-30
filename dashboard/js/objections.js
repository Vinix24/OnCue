(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getObjectionsWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/objections`;
const objectionsPanel = document.getElementById("objections-panel");
const heroHeadline = document.getElementById("monologue-warning");
const MAX_OBJECTIONS = 12;
const HERO_SPOTLIGHT_MS = 12000;
const RESPONSE_LOCKED_TEASER = "\u{1F512} Pro: bekijk de beste tegenreactie";

// True while the hero headline is owned by an objection/opportunity spotlight
// (vs. the default calm line or a coaching/monologue message from app.js).
// Exposed on window so opportunities.js and app.js can share the ownership
// flag across IIFE boundaries.
window._heroSpotlightOwnedByObjection = false;

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

// Confidence tier. Before #205 every keyword match came back at a hardcoded
// 0.95/"high", so rendering the tier would have rendered a constant; the
// score is now proportional to real overlap and the tier is derived from the
// detector's own configured thresholds (detector-status.js owns them, fed by
// the /ws/detector-status payload). A weak match must not look like a strong
// one -- see .is-uncertain in dashboard.css.
const applyConfidenceTier = (card, confidence) => {
  const tier = typeof window.detectorConfidenceTier === "function"
    ? window.detectorConfidenceTier(confidence)
    : "unknown";
  if (tier === "unknown") {
    return;
  }
  card.classList.add(tier === "high" ? "is-high" : "is-uncertain");
  card.title = window.t(tier === "high" ? "detector.confidence_high" : "detector.confidence_uncertain");
};

const clearEmptyState = () => {
  if (!objectionsPanel) {
    return;
  }
  const empty = objectionsPanel.querySelector(".pain-points-empty");
  if (empty) {
    objectionsPanel.removeChild(empty);
  }
};

const collapseOlderCards = (latestCard) => {
  if (!objectionsPanel) {
    return;
  }
  Array.from(objectionsPanel.querySelectorAll(".pain-point-card")).forEach((card) => {
    const isLatest = card === latestCard;
    card.classList.toggle("is-expanded", isLatest);
    card.classList.toggle("is-collapsed", !isLatest);
  });
};

const pruneCards = () => {
  if (!objectionsPanel) {
    return;
  }
  const cards = objectionsPanel.querySelectorAll(".pain-point-card");
  if (cards.length <= MAX_OBJECTIONS) {
    return;
  }
  for (let index = MAX_OBJECTIONS; index < cards.length; index += 1) {
    objectionsPanel.removeChild(cards[index]);
  }
};

const resetHeroSpotlight = () => {
  if (!heroHeadline || !window._heroSpotlightOwnedByObjection) {
    return;
  }
  window._heroSpotlightOwnedByObjection = false;
  if (heroHeadline.hideTimer) {
    clearTimeout(heroHeadline.hideTimer);
    heroHeadline.hideTimer = null;
  }
  heroHeadline.classList.remove("visible");
  heroHeadline.style.color = "";
  heroHeadline.textContent = window.t("coaching.monologue_default");
  window.hintFeedback?.hideHero();
};

/**
 * Route a fresh objection to the top hero spotlight as the immediate
 * "say this now" cue: objection category + counter-response, verbatim from
 * the payload. The Pro-lock is respected — when response_suggestion is the
 * RESPONSE_LOCKED_TEASER, the teaser itself is shown, never a fabricated
 * answer.
 *
 * When response_suggestion is empty (no curated response for this category),
 * the hero still spotlights the category + trigger phrase so it is never
 * stuck on the idle placeholder while cards fill the side panels.
 *
 * Precedence: the latest signal wins. The objection holds the spotlight for
 * HERO_SPOTLIGHT_MS and then the hero returns to its calm default line; the
 * next coaching/monologue event (app.js) or a new objection/opportunity
 * overwrites it sooner — both modules share the headline element's hideTimer
 * handle, so whichever fires last owns the hero. Calm by design: no alerts,
 * popups or flashing, just the same spotlight styling a coaching signal uses.
 */
const showObjectionInHero = (payload) => {
  if (!heroHeadline) {
    return;
  }
  // When there is a response_suggestion, spotlight it as the "best next move".
  // When curation is empty, still spotlight the category + trigger phrase so
  // the hero is never stuck on the idle placeholder while cards fill up.
  const spotlight = payload.response_suggestion
    ? `${formatCategory(payload.category)}: ${payload.response_suggestion}`
    : `⚠ ${formatCategory(payload.category)}${payload.trigger_phrase ? `: "${payload.trigger_phrase}"` : ""}`;
  window._heroSpotlightOwnedByObjection = true;
  heroHeadline.textContent = spotlight;
  window.hintFeedback?.showHero({
    hint: payload.response_suggestion || payload.trigger_phrase || formatCategory(payload.category),
    contextUtterance: payload.trigger_phrase || "",
    timestampMs: payload.timestamp_ms,
  });
  heroHeadline.style.color = "";
  heroHeadline.classList.add("visible");
  if (heroHeadline.hideTimer) {
    clearTimeout(heroHeadline.hideTimer);
  }
  heroHeadline.hideTimer = setTimeout(resetHeroSpotlight, HERO_SPOTLIGHT_MS);
};

const addObjectionCard = (payload) => {
  if (!payload || payload.type !== "objection" || !objectionsPanel) {
    return;
  }

  showObjectionInHero(payload);
  clearEmptyState();

  const card = document.createElement("div");
  card.className = "pain-point-card is-expanded is-new";
  applyConfidenceTier(card, payload.confidence);

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

  const response = document.createElement("div");
  response.className = "pain-point-case";
  if (payload.response_suggestion === RESPONSE_LOCKED_TEASER) {
    response.classList.add("is-locked");
  }
  response.textContent = payload.response_suggestion || window.t("common.no_suggestion_available");

  card.appendChild(header);
  card.appendChild(quote);
  card.appendChild(response);

  if (payload.response_suggestion && window.createHintFeedbackControl) {
    card.appendChild(window.createHintFeedbackControl({
      hint: payload.response_suggestion,
      contextUtterance: payload.trigger_phrase || "",
      timestampMs: payload.timestamp_ms,
    }));
  }

  objectionsPanel.prepend(card);
  collapseOlderCards(card);
  pruneCards();

  setTimeout(() => {
    card.classList.remove("is-new");
  }, 600);
};

// Always reset the hero text on call-end, regardless of spotlight-ownership
// flag. A stale coaching message or a flag that was cleared by showWarning
// should not leave the hero showing the wrong text after the call resets.
window.resetObjectionsPanel = () => {
  if (heroHeadline) {
    if (heroHeadline.hideTimer) {
      clearTimeout(heroHeadline.hideTimer);
      heroHeadline.hideTimer = null;
    }
    heroHeadline.classList.remove("visible");
    heroHeadline.style.color = "";
    heroHeadline.textContent = window.t("coaching.monologue_default");
    window.hintFeedback?.hideHero();
  }
  window._heroSpotlightOwnedByObjection = false;
  if (!objectionsPanel) {
    return;
  }
  objectionsPanel.innerHTML = `<div class="pain-points-empty">${window.t("objections.empty_state")}</div>`;
};

if (objectionsPanel) {
  let socket;
  let retryCount = 0;

  const connect = () => {
    socket = new WebSocket(getObjectionsWsUrl());

    socket.addEventListener("message", (event) => {
      try {
        addObjectionCard(JSON.parse(event.data));
      } catch (error) {
        console.error("Invalid objection message", error);
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
