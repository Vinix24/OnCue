(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const wsBase = () => window.WS_BASE_URL || DEFAULT_WS_BASE_URL;

/* ---- view-state: sidebar start/stop button + in-call/post-call body class ----
 * The dashboard no longer has a separate full-screen start-gate: the call
 * dashboard (#call-view) is always visible and the sidebar owns the single
 * start/stop control. setup.js still toggles #call-view / #report-view via
 * inline display styles (unchanged, unedited) — we only add a body class so
 * CSS can show/hide the right sidebar button, and we re-show the dashboard
 * after setup.js hides it (there is no #setup-view to fall back to anymore).
 */
const callView = document.getElementById("call-view");

const wrapView = (name, after) => {
  const native = window[name];
  if (typeof native !== "function") {
    return;
  }
  window[name] = function (...args) {
    const result = native.apply(this, args);
    after();
    return result;
  };
};

wrapView("showCallView", () => {
  document.body.classList.add("in-call");
  document.body.classList.remove("post-call");
});

wrapView("showSetupView", () => {
  document.body.classList.remove("in-call", "post-call");
  if (callView) {
    callView.style.display = "block";
  }
});

wrapView("showReportView", () => {
  document.body.classList.remove("in-call");
  document.body.classList.add("post-call");
});

/* ---- topbar prospect mirror — reflects the real sidebar field, no fake data ----
 * klantmap-als-eenheid D2 replaced the free-typed "Prospect bedrijf" / "Industry"
 * fields with one client picker; industry is no longer known client-side (it comes
 * from klant.yaml, server-side, only once the call actually starts), so the topbar
 * mirror now shows only the picked client's display name.
 */
const clientSelect = document.getElementById("client-select");
const topbarCompany = document.getElementById("topbar-company");
const topbarIndustry = document.getElementById("topbar-industry");

const syncTopbarDeal = () => {
  if (!topbarCompany) {
    return;
  }
  const selectedOption = clientSelect?.options?.[clientSelect.selectedIndex];
  const company = selectedOption?.value ? selectedOption.textContent?.trim() : "";
  topbarCompany.textContent = company || window.t("header.no_prospect_set");
  if (topbarIndustry) {
    topbarIndustry.textContent = "";
  }
};

if (clientSelect) {
  clientSelect.addEventListener("change", syncTopbarDeal);
}
window.SalesCopilotI18n.ready.then(syncTopbarDeal);

/* ---- live panel count badges — derived from real DOM state, not invented data ---- */
const wireCountBadge = (panelId, countId) => {
  const panel = document.getElementById(panelId);
  const badge = document.getElementById(countId);
  if (!panel || !badge) {
    return;
  }
  const update = () => {
    const count = panel.querySelectorAll(".pain-point-card, .opportunity-card").length;
    badge.textContent = String(count);
  };
  new MutationObserver(update).observe(panel, { childList: true });
  update();
};

wireCountBadge("objections-panel", "objections-count");
wireCountBadge("pain-points-panel", "pain-points-count");
wireCountBadge("opportunities-panel", "opportunities-count");

/* ---- Klantstemming gauge — real running tally of live bezwaar/kans events.
 * Not a simulated value: every +1/-1 step is triggered by an actual
 * objection or buying-signal message from the hub. Resets on call
 * boundaries via the same /ws/config channel other panels already use.
 */
const sentKnob = document.getElementById("sent-knob");
const sentNow = document.getElementById("sent-now");

if (sentKnob && sentNow) {
  let warmth = 50;

  const renderGauge = () => {
    sentKnob.style.left = `${warmth}%`;
    sentNow.textContent = warmth > 66
      ? window.t("sentiment.warm")
      : warmth < 34
        ? window.t("sentiment.cool")
        : window.t("sentiment.neutral");
  };

  const nudge = (delta) => {
    warmth = Math.max(6, Math.min(94, warmth + delta));
    renderGauge();
  };

  const resetGauge = () => {
    warmth = 50;
    renderGauge();
  };

  sentKnob.style.left = `${warmth}%`;
  window.SalesCopilotI18n.ready.then(renderGauge);

  const connectReconnecting = (path, onMessage) => {
    let retryCount = 0;
    const connect = () => {
      const socket = new WebSocket(`${wsBase()}/ws/${path}`);
      socket.addEventListener("message", (event) => {
        try {
          onMessage(JSON.parse(event.data));
        } catch (error) {
          console.error(`Invalid ${path} message`, error);
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
      socket.addEventListener("error", () => socket.close());
    };
    connect();
  };

  connectReconnecting("objections", (payload) => {
    if (payload?.type === "objection") {
      nudge(-8);
    }
  });
  connectReconnecting("buying-signals", (payload) => {
    if (payload?.type === "buying_signal") {
      nudge(10);
    }
  });
  connectReconnecting("config", (payload) => {
    if (payload?.type === "start_call" || payload?.type === "call_ended") {
      resetGauge();
    }
  });
}
})();
