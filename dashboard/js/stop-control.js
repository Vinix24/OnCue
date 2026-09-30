/**
 * Server-stop control (rare, destructive action).
 *
 * Distinct from the daily "Stop & rapport" (#end-call, handled in report.js),
 * which ends the current call and leaves the server running. This button shuts
 * the whole OnCue server down via /api/shutdown.
 *
 * The dashboard never uses browser confirm()/alert() dialogs — they block the
 * page and don't match the rest of the UI. Instead this opens an in-dashboard
 * confirm modal (same overlay pattern as the pro-upgrade modal). On confirm it
 * POSTs /api/shutdown and shows a full-screen shutdown panel that explains how
 * to restart OnCue and refresh this tab.
 */
(() => {
const triggerBtn = document.getElementById("stop-server-btn");
const modal = document.getElementById("stop-server-confirm-modal");
const confirmBtn = document.getElementById("stop-server-confirm-btn");
const cancelBtn = document.getElementById("stop-server-cancel-btn");
const screen = document.getElementById("stop-server-screen");

// Trigger button may be absent in partial/embedded mounts; the modal and
// screen elements always exist in the full dashboard.
if (!triggerBtn || !modal || !confirmBtn || !cancelBtn || !screen) {
  return;
}

let shuttingDown = false;

const openModal = () => {
  modal.classList.remove("hidden");
  // Focus the safe (cancel) option first — the action is destructive.
  cancelBtn.focus();
};

const closeModal = () => {
  modal.classList.add("hidden");
  if (triggerBtn) {
    triggerBtn.focus();
  }
};

if (triggerBtn) {
  triggerBtn.addEventListener("click", openModal);
}

if (cancelBtn) {
  cancelBtn.addEventListener("click", closeModal);
}

// Keyboard: Escape cancels (matches the non-blocking intent). Enter on the
// confirm button proceeds — handled natively by the focused button.
modal.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeModal();
  }
});

if (confirmBtn) {
  confirmBtn.addEventListener("click", async () => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    modal.classList.add("hidden");

    try {
      await window.copilotAuthReady;
      await fetch("/api/shutdown", {
        method: "POST",
        headers: { ...window.copilotAuthHeaders() },
      });
    } catch (_e) {
      // expected: server stops before the response arrives
    }

    // The shutdown screen is a real DOM element (hidden by default). Show it
    // instead of overwriting document.body — keeps styling consistent and
    // points the user at how to come back. The dashboard underneath is inert.
    screen.classList.remove("hidden");
    // Move focus onto the screen for screen-reader users.
    screen.focus();
  });
}
})();
