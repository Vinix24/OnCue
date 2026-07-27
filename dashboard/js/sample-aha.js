(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";

const resolveApiBaseUrl = () => {
  if (window.API_BASE_URL && String(window.API_BASE_URL).trim()) {
    return String(window.API_BASE_URL).trim().replace(/\/$/, "");
  }
  return "";
};

const resolveWsBaseUrl = () => window.WS_BASE_URL || DEFAULT_WS_BASE_URL;

const button = document.getElementById("sample-aha");
const setupStatus = document.querySelector(".setup-status");
const originalLabel = button ? button.textContent : "Toon me wat het doet";

let configSocket = null;

const setButtonLoading = (isLoading) => {
  if (!button) {
    return;
  }
  if (isLoading) {
    button.disabled = true;
    button.textContent = window.t("setup.sample_aha_playing");
  } else {
    button.disabled = false;
    button.textContent = originalLabel;
  }
};

const setStatus = (text) => {
  if (setupStatus) {
    setupStatus.textContent = text;
  }
};

const closeConfigSocket = () => {
  if (configSocket) {
    configSocket.close();
    configSocket = null;
  }
};

const onSampleEnded = () => {
  closeConfigSocket();
  if (window.stopCallSockets) {
    window.stopCallSockets();
  }
  if (window.showSetupView) {
    window.showSetupView();
  }
  setButtonLoading(false);
  setStatus(window.t("setup.status_ready"));
};

const listenForEndCall = () => {
  closeConfigSocket();
  try {
    configSocket = new WebSocket(`${resolveWsBaseUrl()}/ws/config`);

    configSocket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload && payload.type === "end_call") {
          onSampleEnded();
        }
      } catch (error) {
        console.error("Invalid config message", error);
      }
    });

    configSocket.addEventListener("close", () => {
      configSocket = null;
    });

    configSocket.addEventListener("error", () => {
      configSocket = null;
    });
  } catch (error) {
    console.error("Could not listen for end_call", error);
  }
};

const startSampleAha = async () => {
  if (!button || button.disabled) {
    return;
  }

  setButtonLoading(true);
  setStatus(window.t("setup.sample_aha_loading"));

  const apiBaseUrl = resolveApiBaseUrl();
  const url = apiBaseUrl ? `${apiBaseUrl}/api/sample-aha/start` : "/api/sample-aha/start";

  try {
    await window.copilotAuthReady;
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...window.copilotAuthHeaders(),
      },
      body: JSON.stringify({ speed: 1.5 }),
    });

    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new Error(window.t("setup.sample_aha_start_failed", { status: response.status, detail }));
    }

    setStatus(window.t("setup.sample_aha_running"));
    if (window.showCallView) {
      window.showCallView();
    }
    if (window.startCallSockets) {
      window.startCallSockets();
    }
    listenForEndCall();
  } catch (error) {
    console.error("Sample aha failed", error);
    window.alert(error.message || window.t("setup.sample_aha_failed_generic"));
    closeConfigSocket();
    setButtonLoading(false);
    setStatus(window.t("setup.status_ready"));
  }
};

if (button) {
  button.addEventListener("click", startSampleAha);
}
})();
