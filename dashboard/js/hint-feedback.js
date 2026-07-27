/**
 * User-facing "card used" feedback control (thumbs up / thumbs down) for
 * coaching hints — the UI half of the learning loop. Each vote POSTs to the
 * hub's existing /api/hint-feedback endpoint (see
 * src/sales_copilot/websocket/hub_api.py HintFeedbackRequest), which appends
 * a HintFeedbackRecord to data/feedback/hints.ndjson for eval-set input.
 *
 * Mirrors the payload shape of dashboard/tester.html's submitHintFeedback(),
 * authenticating with the shared session token (js/copilot-auth.js) exactly
 * like every other mutating dashboard call.
 *
 * Used by:
 *   - pain-points.js / objections.js / opportunities.js: one control per
 *     rendered card that carries a response_suggestion.
 *   - app.js + objections.js hero cue: window.hintFeedback.showHero/hideHero
 *     render the same control into the #hero-feedback host next to the
 *     coaching spotlight.
 *
 * Calm by design: no popups, no alerts. A failed POST is logged quietly and
 * the highlight simply does not stick.
 */
(() => {
const ENDPOINT = "/api/hint-feedback";

/**
 * Exact HintFeedbackRequest payload: hint + feedback ('up'|'down') are
 * required; context_utterance(s) and timestamp_ms are the optional context
 * fields the schema accepts. The hub derives session/phase itself when left
 * out.
 */
const buildPayload = ({ hint, feedback, contextUtterance = "", timestampMs = null }) => {
  const context = String(contextUtterance || "").trim();
  const payload = {
    hint: String(hint || "").trim(),
    feedback, // exactly "up" or "down" — the only values the endpoint accepts
    context_utterance: context,
    context_utterances: context ? [context] : [],
  };
  if (typeof timestampMs === "number" && !Number.isNaN(timestampMs)) {
    payload.timestamp_ms = Math.round(timestampMs);
  }
  return payload;
};

const postVote = async (payload) => {
  await (window.copilotAuthReady || Promise.resolve());
  const headers = { "Content-Type": "application/json" };
  if (typeof window.copilotAuthHeaders === "function") {
    Object.assign(headers, window.copilotAuthHeaders());
  }
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`hint-feedback POST failed: HTTP ${response.status}`);
  }
};

/**
 * Create a small thumbs-up / thumbs-down control for one coaching hint.
 * Voting is idempotent on the UI side: the chosen thumb is highlighted via
 * the host's data-vote attribute and re-clicking (or switching sides) just
 * records a new vote and updates the highlight — nothing duplicates.
 */
const createHintFeedbackControl = ({ hint, contextUtterance = "", timestampMs = null } = {}) => {
  const host = document.createElement("div");
  host.className = "hint-feedback";

  const makeButton = (dir, labelKey, ariaKey, glyph) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "hint-feedback-btn";
    button.dataset.dir = dir;
    button.setAttribute("aria-label", window.t(ariaKey));
    button.textContent = `${glyph} ${window.t(labelKey)}`;
    button.addEventListener("click", () => {
      postVote(buildPayload({ hint, feedback: dir, contextUtterance, timestampMs }))
        .then(() => {
          host.dataset.vote = dir;
        })
        .catch((error) => {
          // Quiet failure: the vote is lost, the UI stays calm.
          console.warn("hint feedback not saved:", error.message);
        });
    });
    return button;
  };

  host.appendChild(makeButton("up", "feedback.useful_label", "feedback.useful_aria", "👍"));
  host.appendChild(makeButton("down", "feedback.not_useful_label", "feedback.not_useful_aria", "👎"));
  return host;
};

const heroHost = () => document.getElementById("hero-feedback");

/** Show a fresh feedback control under the hero coaching cue. */
const showHero = (options) => {
  const host = heroHost();
  if (!host) {
    return;
  }
  host.innerHTML = "";
  host.appendChild(createHintFeedbackControl(options));
  host.hidden = false;
};

/** Clear the hero feedback control when the hero returns to its calm line. */
const hideHero = () => {
  const host = heroHost();
  if (!host) {
    return;
  }
  host.hidden = true;
  host.innerHTML = "";
};

window.createHintFeedbackControl = createHintFeedbackControl;
window.hintFeedback = { showHero, hideHero };
})();
