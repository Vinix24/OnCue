/**
 * Pro-feature gating for the coaching dashboard.
 *
 * Responsibilities:
 * - Fetch /api/v1/license/features once on load; cache the result.
 * - Render the Gespreksmedium custom control (#call-medium-ui) so that:
 *     Free users: Telefoon option shows a 🔒 Pro badge, is visually dimmed,
 *                  clicking opens the upgrade modal (does NOT change the selection).
 *     Pro/Ent:    Both options are fully selectable; no badge shown.
 * - Keep the hidden <select id="call-medium"> in sync so existing setup.js
 *   wiring (collectConfig / restoreConfig / saveConfig) is unaffected.
 * - Manage the #pro-upgrade-modal: open, close, focus-trap, Esc, backdrop.
 */

// PRO_UPGRADE_URL is defined once in constants.js (loaded before this file).
const PRO_UPGRADE_URL = window.PRO_UPGRADE_URL;

(() => {
  const LICENSE_FEATURES_ENDPOINT = "/api/v1/license/features";
  const FOCUSABLE_SELECTOR =
    'a[href], button:not([disabled]), input:not([disabled]), textarea, select:not([tabindex="-1"]), [tabindex]:not([tabindex="-1"])';

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  /** Resolved once the feature fetch completes (or fails gracefully). */
  let hasCalltap = false;

  /** True when the Pro entitlement "presentation.dynamic_slides" is active. */
  let hasDynamicSlides = false;

  /** True when the Pro entitlement "coaching.script_tracking" is active. */
  let hasScriptTracking = false;

  /** True when the Pro entitlement "coaching.deep_insights" is active. */
  let hasDeepInsights = false;

  /** The element that had focus before the upgrade modal was opened. */
  let _preModalFocus = null;

  // ---------------------------------------------------------------------------
  // DOM references
  // ---------------------------------------------------------------------------

  const callMediumSelect = document.getElementById("call-medium");
  const callMediumUi = document.getElementById("call-medium-ui");
  const upgradeModal = document.getElementById("pro-upgrade-modal");
  const closeUpgradeBtn = document.getElementById("close-pro-upgrade-modal");
  const laterBtn = document.getElementById("pro-upgrade-later");
  const upgradeCta = document.getElementById("pro-upgrade-cta");

  // ---------------------------------------------------------------------------
  // Feature fetch — follows the same safeJson / resolveApiBaseUrl pattern as
  // setup.js so error handling is consistent across all dashboard fetches.
  // ---------------------------------------------------------------------------

  const safeJson = async (response) => {
    try {
      return await response.json();
    } catch (_e) {
      return null;
    }
  };

  const fetchLicenseFeatures = async () => {
    try {
      const response = await fetch(LICENSE_FEATURES_ENDPOINT);
      if (!response.ok) {
        return;
      }
      const data = await safeJson(response);
      if (!data || !Array.isArray(data.features)) {
        return;
      }
      hasCalltap = data.features.includes("audio.calltap");
      hasDynamicSlides = data.features.includes("presentation.dynamic_slides");
      hasScriptTracking = data.features.includes("coaching.script_tracking");
      hasDeepInsights = data.features.includes("coaching.deep_insights");
    } catch (_e) {
      // Server unreachable — stay in Free-gated mode (safe default).
    }
  };

  // ---------------------------------------------------------------------------
  // Upgrade modal
  // ---------------------------------------------------------------------------

  const openUpgradeModal = () => {
    if (!upgradeModal) {
      return;
    }
    _preModalFocus = document.activeElement;
    upgradeModal.classList.remove("hidden");
    // Move focus to the first focusable element inside the modal.
    const first = upgradeModal.querySelector(FOCUSABLE_SELECTOR);
    if (first) {
      first.focus();
    }
  };

  const closeUpgradeModal = () => {
    if (!upgradeModal) {
      return;
    }
    upgradeModal.classList.add("hidden");
    // Restore focus to the element that triggered the modal.
    if (_preModalFocus && typeof _preModalFocus.focus === "function") {
      _preModalFocus.focus();
    }
    _preModalFocus = null;
  };

  const trapFocus = (event) => {
    if (!upgradeModal || upgradeModal.classList.contains("hidden")) {
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const focusable = Array.from(upgradeModal.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
      (el) => !el.closest(".hidden"),
    );
    if (!focusable.length) {
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const initUpgradeModal = () => {
    if (!upgradeModal) {
      return;
    }

    // Ensure the upgrade URL is applied to the CTA anchor.
    if (upgradeCta) {
      upgradeCta.href = PRO_UPGRADE_URL;
    }

    if (closeUpgradeBtn) {
      closeUpgradeBtn.addEventListener("click", closeUpgradeModal);
    }
    if (laterBtn) {
      laterBtn.addEventListener("click", closeUpgradeModal);
    }

    // Backdrop click closes modal.
    upgradeModal.addEventListener("click", (event) => {
      if (event.target === upgradeModal) {
        closeUpgradeModal();
      }
    });

    // Esc key closes modal.
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !upgradeModal.classList.contains("hidden")) {
        closeUpgradeModal();
      }
    });

    // Focus trap.
    upgradeModal.addEventListener("keydown", trapFocus);
  };

  // ---------------------------------------------------------------------------
  // Custom Gespreksmedium control
  // ---------------------------------------------------------------------------

  /**
   * Sync the hidden select value so setup.js collectConfig() reads the right
   * prospect_source without any code changes on its side.
   */
  const syncSelectValue = (value) => {
    if (!callMediumSelect) {
      return;
    }
    callMediumSelect.value = value;
    // Fire change so setup.js saveConfig() listener picks it up.
    callMediumSelect.dispatchEvent(new Event("change", { bubbles: true }));
  };

  /**
   * Read the current value from the hidden select (used during restoreConfig
   * which sets callMediumSelect.value directly).
   */
  const readSelectValue = () => (callMediumSelect ? callMediumSelect.value : "blackhole");

  /**
   * Apply the active visual state to the medium buttons.
   * Only called after renderCallMediumUi has already built the buttons.
   */
  const updateMediumActiveState = (activeValue) => {
    if (!callMediumUi) {
      return;
    }
    callMediumUi.querySelectorAll(".medium-btn").forEach((btn) => {
      const isActive = btn.dataset.value === activeValue;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-pressed", String(isActive));
    });
  };

  /**
   * Build the visible button-group control that replaces the hidden select.
   * Called once after the feature fetch resolves.
   */
  const renderCallMediumUi = () => {
    if (!callMediumUi) {
      return;
    }

    callMediumUi.innerHTML = "";

    const options = [
      { value: "blackhole", label: window.t("setup.call_medium_video_option"), proGated: false },
      { value: "audiotee_call", label: window.t("setup.call_medium_phone_option"), proGated: !hasCalltap },
    ];

    const currentValue = readSelectValue();

    options.forEach(({ value, label, proGated }) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "medium-btn";
      btn.dataset.value = value;

      if (proGated) {
        btn.classList.add("medium-btn--pro-gated");
        btn.setAttribute("aria-label", `${label} — ${window.t("pro_upgrade.modal_title")}`);

        const labelSpan = document.createElement("span");
        labelSpan.textContent = label;

        const badge = document.createElement("span");
        badge.className = "pro-badge";
        badge.setAttribute("aria-hidden", "true");
        badge.textContent = window.t("pro_upgrade.badge_label");

        btn.appendChild(labelSpan);
        btn.appendChild(badge);

        btn.addEventListener("click", () => {
          // Do NOT change selection — open upgrade modal instead.
          openUpgradeModal();
        });
      } else {
        btn.textContent = label;
        btn.addEventListener("click", () => {
          syncSelectValue(value);
          updateMediumActiveState(value);
        });
      }

      const isActive = value === currentValue && !proGated;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-pressed", String(isActive));

      callMediumUi.appendChild(btn);
    });

    // If the stored value is audiotee_call but user is Free, fall back to
    // blackhole so the hidden select and visual state stay consistent.
    if (!hasCalltap && readSelectValue() === "audiotee_call") {
      syncSelectValue("blackhole");
      updateMediumActiveState("blackhole");
    }
  };

  /**
   * setup.js restoreConfig() directly sets callMediumSelect.value and does NOT
   * call renderCallMediumUi. Observe value changes so the visible control stays
   * in sync after a restore.
   *
   * We do this with a MutationObserver on the select's attribute (restoreConfig
   * sets .value directly) plus an input/change listener.
   */
  const observeSelectSync = () => {
    if (!callMediumSelect) {
      return;
    }
    const syncUi = () => {
      const v = readSelectValue();
      // If Free user somehow got audiotee_call restored, reset it.
      if (!hasCalltap && v === "audiotee_call") {
        callMediumSelect.value = "blackhole";
        updateMediumActiveState("blackhole");
      } else {
        updateMediumActiveState(v);
      }
    };
    callMediumSelect.addEventListener("change", syncUi);
    callMediumSelect.addEventListener("input", syncUi);
  };

  // ---------------------------------------------------------------------------
  // Dynamic slides gating — hide Slide Injector toggle for Free users
  // ---------------------------------------------------------------------------

  /**
   * When the "coaching.script_tracking" entitlement is absent:
   *   - Hide the Script Dekking side-panel card and show a compact upgrade
   *     hint in its place.
   *   - Keep the element in the DOM so tests and layout remain stable.
   */
  const applyScriptTrackingGating = () => {
    const section = document.getElementById("script-tracking-section");
    if (!section) {
      return;
    }
    if (hasScriptTracking) {
      return;
    }

    const panel = document.getElementById("script-tracking-panel");
    if (panel) {
      const message = window.t("scripttracking.pro_gate_message");
      const upgradeLabel = window.t("scripttracking.upgrade_button");
      panel.innerHTML =
        `<div class="script-tracking-empty script-tracking-upgrade">${message} <button type="button" class="script-tracking-upgrade-btn">${upgradeLabel}</button></div>`;
      const upgradeBtn = panel.querySelector(".script-tracking-upgrade-btn");
      if (upgradeBtn) {
        upgradeBtn.addEventListener("click", openUpgradeModal);
      }
    }
  };

  /**
   * When the "coaching.deep_insights" entitlement is absent:
   *   - Hide the Diepte-inzichten side-panel feed/ask-box and show a compact
   *     upgrade hint in its place.
   *   - Keep the section in the DOM so tests and layout remain stable.
   */
  const applyInsightsGating = () => {
    const section = document.getElementById("insights-section");
    if (!section) {
      return;
    }
    if (hasDeepInsights) {
      return;
    }

    const panel = document.getElementById("insights-panel");
    if (panel) {
      const message = window.t("insights.pro_gate_message");
      const upgradeLabel = window.t("insights.upgrade_button");
      panel.innerHTML =
        `<div class="insights-empty insights-upgrade">${message} <button type="button" class="insights-upgrade-btn">${upgradeLabel}</button></div>`;
      const upgradeBtn = panel.querySelector(".insights-upgrade-btn");
      if (upgradeBtn) {
        upgradeBtn.addEventListener("click", openUpgradeModal);
      }
    }
    const askForm = document.getElementById("insights-ask-form");
    if (askForm) {
      askForm.hidden = true;
    }
  };

  /**
   * When the "presentation.dynamic_slides" entitlement is absent:
   *   - Hide the Slide Injector module-toggle row entirely (display:none via
   *     the element's hidden attribute) so Free users never see it.
   *   - Uncheck the underlying checkbox so collectConfig() in setup.js does
   *     not enable the presentation module.
   *   - Shorten the setup subtitle so it no longer advertises slides.
   *
   * When entitled: leave all elements untouched (toggle visible + checked,
   * original subtitle).
   *
   * The DOM elements are NOT removed so existing HTML structure and tests
   * remain intact.
   */
  const applyDynamicSlidesGating = () => {
    const presentationCheckbox = document.querySelector(
      'input[type="checkbox"][data-module="presentation"]',
    );
    const setupSubtitle = document.getElementById("setup-subtitle");

    if (hasDynamicSlides) {
      // Entitled — nothing to hide or change.
      return;
    }

    // Hide the entire toggle row (the wrapping <label class="module-toggle">).
    if (presentationCheckbox) {
      const row = presentationCheckbox.closest(".module-toggle");
      if (row) {
        row.hidden = true;
      }
      // Uncheck so collectConfig() does not send presentation: true.
      presentationCheckbox.checked = false;
    }

    // Drop "en slides" from the subtitle.
    if (setupSubtitle) {
      setupSubtitle.textContent = window.t("setup.subtitle_no_slides");
    }
  };

  // ---------------------------------------------------------------------------
  // Init sequence
  // ---------------------------------------------------------------------------

  const init = async () => {
    await fetchLicenseFeatures();
    await window.SalesCopilotI18n.ready;
    initUpgradeModal();
    renderCallMediumUi();
    observeSelectSync();
    applyDynamicSlidesGating();
    applyScriptTrackingGating();
    applyInsightsGating();
  };

  // Run after all other scripts have loaded (setup.js runs on DOMContentLoaded
  // equivalent via its own IIFE; waiting for load ensures the hidden select
  // value has been restored before we read it).
  if (document.readyState === "complete") {
    init();
  } else {
    window.addEventListener("load", init);
  }
})();
