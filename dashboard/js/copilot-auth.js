/**
 * Fetches the session token from the hub so the dashboard can authenticate
 * mutating API calls (start-call, end-call, upload, shutdown).
 *
 * The hub always requires this token for mutating API calls.
 */
window._copilotToken = '';

window.copilotAuthReady = (async () => {
  try {
    const resp = await fetch('/api/v1/auth/session-token');
    if (resp.ok) {
      const data = await resp.json();
      window._copilotToken = (data && data.token) ? data.token : '';
    }
  } catch (_e) {
    // Hub not reachable yet — mutating calls will fail closed.
  }
})();

/** Returns headers object with X-Sales-Copilot-Token for mutating requests. */
window.copilotAuthHeaders = function () {
  if (!window._copilotToken) return {};
  return { 'X-Sales-Copilot-Token': window._copilotToken };
};
