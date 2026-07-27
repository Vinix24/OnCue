(() => {
const CONSENT_STORAGE_KEY = "sales-copilot-consent";
const CONFIG_ENDPOINT = "/api/config";

const elements = {
  card: document.getElementById("consent-card"),
  checkbox: document.getElementById("consent-given"),
  badge: document.getElementById("consent-tier-badge"),
  status: document.getElementById("consent-status"),
  hint: document.getElementById("consent-hint"),
  startCall: document.getElementById("start-call"),
};

let activeTier = "audit";

const safeJson = async (response) => {
  try {
    return await response.json();
  } catch (error) {
    return null;
  }
};

const tierLabelKeys = {
  off: "consent.tier_badge_off",
  audit: "consent.tier_badge_audit",
  soft: "consent.tier_badge_soft",
  strict: "consent.tier_badge_strict",
};

const updateIndicator = () => {
  if (!elements.badge || !elements.status) {
    return;
  }
  const tierKey = tierLabelKeys[activeTier];
  elements.badge.textContent = tierKey ? window.t(tierKey) : activeTier;
  elements.badge.className = `consent-tier-badge tier-${activeTier}`;
  const given = !!elements.checkbox?.checked;
  if (given) {
    elements.status.textContent = window.t("consent.status_recorded");
    elements.status.classList.remove("is-missing");
    elements.status.classList.add("is-recorded");
  } else {
    elements.status.textContent = window.t("consent.status_not_recorded");
    elements.status.classList.remove("is-recorded");
    elements.status.classList.add("is-missing");
  }
  if (elements.hint) {
    if (activeTier === "strict") {
      elements.hint.textContent = given
        ? window.t("consent.hint_strict_given")
        : window.t("consent.hint_strict_missing");
      elements.hint.classList.toggle("is-warning", !given);
    } else if (activeTier === "soft") {
      elements.hint.textContent = given
        ? window.t("consent.hint_soft_given")
        : window.t("consent.hint_soft_missing");
      elements.hint.classList.toggle("is-warning", !given);
    } else {
      elements.hint.textContent = "";
      elements.hint.classList.remove("is-warning");
    }
  }
  if (elements.startCall && activeTier === "strict") {
    elements.startCall.disabled = !given;
  }
};

const setVisible = (visible) => {
  if (!elements.card) {
    return;
  }
  elements.card.classList.toggle("hidden", !visible);
};

const applyTier = (tier) => {
  activeTier = tier || "audit";
  if (activeTier === "off" || activeTier === "audit") {
    setVisible(false);
  } else {
    setVisible(true);
    updateIndicator();
  }
};

const loadConsentState = () => {
  if (!elements.checkbox) {
    return;
  }
  const raw = localStorage.getItem(CONSENT_STORAGE_KEY);
  try {
    const state = raw ? JSON.parse(raw) : null;
    if (state && typeof state.given === "boolean") {
      elements.checkbox.checked = state.given;
    }
  } catch {
    localStorage.removeItem(CONSENT_STORAGE_KEY);
  }
};

const saveConsentState = () => {
  if (!elements.checkbox) {
    return;
  }
  localStorage.setItem(
    CONSENT_STORAGE_KEY,
    JSON.stringify({ given: elements.checkbox.checked })
  );
};

const fetchConsentConfig = async () => {
  try {
    const response = await fetch(CONFIG_ENDPOINT);
    const config = await safeJson(response);
    await window.SalesCopilotI18n.ready;
    if (config && typeof config.consent_tier === "string") {
      applyTier(config.consent_tier);
    }
  } catch {
    applyTier("audit");
  }
};

const initConsent = () => {
  if (!elements.checkbox) {
    return;
  }
  loadConsentState();
  elements.checkbox.addEventListener("change", () => {
    saveConsentState();
    updateIndicator();
  });
  fetchConsentConfig();
};

window.getConsentPayload = () => {
  if (!elements.checkbox) {
    return null;
  }
  const given = !!elements.checkbox.checked;
  if (activeTier === "off" || activeTier === "audit") {
    return given ? { asked: true, given: true } : null;
  }
  return { asked: true, given };
};

window.resetConsentState = () => {
  if (!elements.checkbox) {
    return;
  }
  elements.checkbox.checked = false;
  saveConsentState();
  updateIndicator();
};

initConsent();
})();
