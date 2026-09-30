(() => {
const PRESET_ENDPOINT = "/api/presets";
const LLM_MODELS_ENDPOINT = "/api/llm-models";
const CLIENTS_ENDPOINT = "/api/v1/clients";
const STORAGE_KEY = "sales-copilot-setup";
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const resolveApiBaseUrl = () => {
  if (window.API_BASE_URL && String(window.API_BASE_URL).trim()) {
    return String(window.API_BASE_URL).trim().replace(/\/$/, "");
  }
  return "";
};

const setupSelectors = {
  setupView: document.getElementById("setup-view"),
  callView: document.getElementById("call-view"),
  reportView: document.getElementById("report-view"),
  presetBar: document.getElementById("preset-bar"),
  domainPreset: document.getElementById("domain-preset"),
  screenToggle: document.getElementById("screen-mode-toggle"),
  moduleInputs: Array.from(document.querySelectorAll(".module-toggle input")),
  llmModelOption: document.getElementById("llm-model-option"),
  transcriptBackend: document.getElementById("transcript-backend"),
  callMedium: document.getElementById("call-medium"),
  transcriberSelfLive: document.getElementById("transcribe-self-live"),
  detectionOffIndicator: document.getElementById("detection-off-indicator"),
  clientSelect: document.getElementById("client-select"),
  cloudSyncWarning: document.getElementById("client-cloud-sync-warning"),
  dropzone: document.getElementById("document-dropzone"),
  fileInput: document.getElementById("document-input"),
  fileList: document.getElementById("document-list"),
  startCall: document.getElementById("start-call"),
  setupStatus: document.querySelector(".setup-status"),
};

let uploadedFiles = [];
let presets = [];
let llmModelOptions = [];
let pendingLlmSelection = null;
let clients = [];
let pendingClientSelection = null;

// pain_points is the AI-detection master switch. Both `applyPreset` and
// `restoreConfig` write module checkboxes and both must go through
// writeModuleCheckboxes() below so the same rule applies everywhere:
// a deliberate operator action (a preset click, a restored prior session)
// may set any module -- including turning the detector off, which
// `discovery` / `coaching_only` do on purpose. What must never happen is a
// *non-deliberate* default (the first preset auto-applied on page load,
// before the operator has done anything) silently overwriting a choice
// that is already established. See dispatch D-543177d3.
//
// The flag also gates `setScreenMode`, which #206 left outside it: the screen
// mode carries a module side effect (single-screen clears `presentation`) and
// is itself persisted, so a non-deliberate write there reached the same state
// by another road.
let modulesEstablished = false;

const MASTER_MODULE = "pain_points";

const writeModuleCheckboxes = (modules, { deliberate }) => {
  if (!modules || !setupSelectors.moduleInputs.length) {
    return;
  }
  if (!deliberate && modulesEstablished) {
    updateDetectionIndicator();
    return;
  }
  setupSelectors.moduleInputs.forEach((input) => {
    const value = modules[input.dataset.module];
    if (typeof value === "boolean") {
      input.checked = value;
    }
  });
  modulesEstablished = true;
  updateDetectionIndicator();
};

// The indicator reads the live checkbox state -- the exact same input
// collectModules() reads to build the start_call payload -- so it can never
// drift from what is actually sent.
const updateDetectionIndicator = () => {
  if (!setupSelectors.detectionOffIndicator) {
    return;
  }
  const painPointsInput = setupSelectors.moduleInputs.find((input) => input.dataset.module === MASTER_MODULE);
  const detectionOn = painPointsInput ? !!painPointsInput.checked : true;
  setupSelectors.detectionOffIndicator.classList.toggle("hidden", detectionOn);
};

const slugifyCompany = (value) => {
  const normalized = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || "default";
};

const safeJson = async (response) => {
  try {
    return await response.json();
  } catch (error) {
    return null;
  }
};

const loadPresets = async () => {
  if (!setupSelectors.presetBar) {
    return;
  }
  try {
    const apiBaseUrl = resolveApiBaseUrl();
    const presetsUrl = apiBaseUrl ? `${apiBaseUrl}${PRESET_ENDPOINT}` : PRESET_ENDPOINT;
    const response = await fetch(presetsUrl);
    const data = await safeJson(response);
    presets = Array.isArray(data) ? data : data?.presets || [];
  } catch (error) {
    presets = [];
  }
  await window.SalesCopilotI18n.ready;
  renderPresets();
};

const llmOptionValue = (provider, model) => `${provider}::${model}`;

const parseLlmOptionValue = (value) => {
  if (!value || typeof value !== "string") {
    return { provider: "", model: "" };
  }
  const [provider, model] = value.split("::");
  return {
    provider: provider || "",
    model: model || "",
  };
};

const applyLlmSelection = (provider, model) => {
  if (!setupSelectors.llmModelOption || !provider || !model) {
    return;
  }
  const selectedValue = llmOptionValue(provider, model);
  const exists = llmModelOptions.some((entry) => entry.value === selectedValue);
  if (exists) {
    setupSelectors.llmModelOption.value = selectedValue;
    pendingLlmSelection = null;
    return;
  }
  pendingLlmSelection = { provider, model };
};

const renderLlmModelOptions = (providers) => {
  if (!setupSelectors.llmModelOption) {
    return;
  }
  setupSelectors.llmModelOption.innerHTML = "";
  llmModelOptions = [];

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = window.t("setup.llm_model_placeholder");
  setupSelectors.llmModelOption.appendChild(placeholder);

  if (!providers || typeof providers !== "object") {
    return;
  }

  Object.entries(providers).forEach(([providerKey, providerConfig]) => {
    if (!providerConfig || typeof providerConfig !== "object" || !Array.isArray(providerConfig.models)) {
      return;
    }
    const providerLabel = providerConfig.label || providerKey;
    providerConfig.models.forEach((modelName) => {
      if (typeof modelName !== "string" || !modelName.trim()) {
        return;
      }
      const value = llmOptionValue(providerKey, modelName);
      llmModelOptions.push({ value, provider: providerKey, model: modelName });
      const option = document.createElement("option");
      option.value = value;
      option.textContent = `${providerLabel} · ${modelName}`;
      setupSelectors.llmModelOption.appendChild(option);
    });
  });

  if (pendingLlmSelection) {
    applyLlmSelection(pendingLlmSelection.provider, pendingLlmSelection.model);
  }
};

const loadLlmModelOptions = async () => {
  const apiBaseUrl = resolveApiBaseUrl();
  const url = apiBaseUrl ? `${apiBaseUrl}${LLM_MODELS_ENDPOINT}` : LLM_MODELS_ENDPOINT;
  let providers = null;
  try {
    const response = await fetch(url);
    const payload = await safeJson(response);
    providers = payload?.providers ?? null;
  } catch (error) {
    providers = null;
  }
  await window.SalesCopilotI18n.ready;
  renderLlmModelOptions(providers);
};

// klantmap-als-eenheid D2: "Server leidt af, dashboard kiest alleen" -- this picker is
// the only client-related control left in the setup menu. It replaces the old free-typed
// "Prospect bedrijf" / "Industry" fields and the "Klantdossier" checkbox; /api/start-call
// derives prospect_company, prospect_industry, call_terms and privacy from the picked
// client's klant.yaml.
const applyClientSelection = (slug) => {
  if (!setupSelectors.clientSelect) {
    return;
  }
  const exists = clients.some((entry) => entry.slug === slug);
  if (exists) {
    setupSelectors.clientSelect.value = slug;
    pendingClientSelection = null;
    return;
  }
  pendingClientSelection = slug;
};

const renderCloudSyncWarning = (warning) => {
  if (!setupSelectors.cloudSyncWarning) {
    return;
  }
  if (!warning || !warning.message) {
    setupSelectors.cloudSyncWarning.textContent = "";
    setupSelectors.cloudSyncWarning.classList.add("hidden");
    return;
  }
  setupSelectors.cloudSyncWarning.textContent = warning.message;
  setupSelectors.cloudSyncWarning.classList.remove("hidden");
};

const renderClientOptions = (clientList) => {
  clients = Array.isArray(clientList) ? clientList : [];
  if (!setupSelectors.clientSelect) {
    return;
  }
  const previousValue = setupSelectors.clientSelect.value;
  setupSelectors.clientSelect.innerHTML = "";

  const noneOption = document.createElement("option");
  noneOption.value = "";
  noneOption.textContent = window.t("setup.client_none_option");
  setupSelectors.clientSelect.appendChild(noneOption);

  clients.forEach((entry) => {
    if (!entry || typeof entry.slug !== "string" || !entry.slug) {
      return;
    }
    const option = document.createElement("option");
    option.value = entry.slug;
    option.textContent = entry.bedrijf || entry.slug;
    setupSelectors.clientSelect.appendChild(option);
  });

  if (pendingClientSelection) {
    applyClientSelection(pendingClientSelection);
  } else if (previousValue) {
    applyClientSelection(previousValue);
  }
};

const loadClients = async () => {
  const apiBaseUrl = resolveApiBaseUrl();
  const url = apiBaseUrl ? `${apiBaseUrl}${CLIENTS_ENDPOINT}` : CLIENTS_ENDPOINT;
  let payload = null;
  try {
    const response = await fetch(url);
    payload = await safeJson(response);
  } catch (error) {
    payload = null;
  }
  await window.SalesCopilotI18n.ready;
  renderClientOptions(payload?.clients);
  renderCloudSyncWarning(payload?.cloud_sync_warning);
};

const renderPresets = () => {
  if (!setupSelectors.presetBar) {
    return;
  }
  setupSelectors.presetBar.innerHTML = "";
  if (!presets.length) {
    const pill = document.createElement("button");
    pill.className = "preset-pill";
    pill.type = "button";
    pill.textContent = window.t("setup.preset_default_label");
    pill.addEventListener("click", () => {
      setActivePreset(pill);
      applyPreset({ name: "Default" });
    });
    setupSelectors.presetBar.appendChild(pill);
    setActivePreset(pill);
    return;
  }
  presets.forEach((preset, index) => {
    const pill = document.createElement("button");
    pill.className = `preset-pill${index === 0 ? " active" : ""}`;
    pill.type = "button";
    pill.textContent = preset.name || preset.label || window.t("setup.preset_fallback_label", { index: index + 1 });
    pill.addEventListener("click", () => {
      setActivePreset(pill);
      applyPreset(preset);
    });
    setupSelectors.presetBar.appendChild(pill);
  });
  if (presets[0]) {
    setActivePreset(setupSelectors.presetBar.querySelector(".preset-pill"));
    applyPreset(presets[0], { deliberate: false });
  }
};

const setActivePreset = (target) => {
  if (!setupSelectors.presetBar) {
    return;
  }
  setupSelectors.presetBar.querySelectorAll(".preset-pill").forEach((button) => {
    button.classList.toggle("active", button === target);
  });
};

const setScreenMode = (mode, { deliberate = true } = {}) => {
  if (!setupSelectors.screenToggle) {
    return;
  }
  // The screen mode belongs to the same established configuration the module
  // checkboxes do -- single-screen clears `presentation` outright, and the mode
  // itself is written straight back to localStorage by the saveConfig() at the
  // end of applyPreset. #206 left this call ungated and its own report flagged
  // the consequence; the leak turned out to be wider than the checkbox alone,
  // because a non-deliberate apply that only flipped the toggle still got
  // persisted and then applied for real by the next deliberate restore. So the
  // whole call passes the guard: a non-deliberate default may establish the
  // mode on a genuinely fresh browser and may never overwrite it afterwards.
  if (!deliberate && modulesEstablished) {
    return;
  }
  setupSelectors.screenToggle.querySelectorAll(".toggle-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  const presentationToggle = setupSelectors.moduleInputs.find((input) => input.dataset.module === "presentation");
  if (!presentationToggle) {
    return;
  }
  // Disabling the input is a capability, not a choice: one screen cannot drive
  // a second one.
  presentationToggle.disabled = mode === "single";
  // Clearing the checkbox is a module write like any other, so it goes through
  // the same choke point rather than around it.
  if (mode === "single") {
    writeModuleCheckboxes({ presentation: false }, { deliberate });
  }
};

const applyPreset = (preset, { deliberate = true } = {}) => {
  if (!preset) {
    return;
  }
  // Resolve the module-write authority ONCE for the whole application. The
  // preset's module set and setScreenMode's single-screen side effect belong
  // to the same apply and must pass or fail the guard together: re-reading
  // modulesEstablished per call would let the first write flip the flag and
  // lock the second one out on a genuinely fresh browser.
  const mayWriteModules = deliberate || !modulesEstablished;
  // Modules first, then the screen mode: the single-screen constraint has to
  // land last so it wins over a preset that also lists presentation:true.
  writeModuleCheckboxes(preset.modules, { deliberate: mayWriteModules });
  if (preset.screen_mode) {
    setScreenMode(preset.screen_mode, { deliberate: mayWriteModules });
  }
  applyLlmSelection(preset.llm_provider, preset.llm_model);
  if (setupSelectors.transcriptBackend && preset.transcript?.backend) {
    setupSelectors.transcriptBackend.value = preset.transcript.backend;
  }
  if (setupSelectors.transcriberSelfLive && typeof preset.transcript?.transcribe_self_live === "boolean") {
    setupSelectors.transcriberSelfLive.checked = preset.transcript.transcribe_self_live;
  }
  saveConfig();
};

const readScreenMode = () => {
  const active = setupSelectors.screenToggle?.querySelector(".toggle-button.active");
  return active?.dataset.mode || "single";
};

const collectModules = () => {
  const modules = {};
  setupSelectors.moduleInputs.forEach((input) => {
    modules[input.dataset.module] = input.checked;
  });
  return modules;
};

const collectConfig = () => {
  const config = {
    preset_name: setupSelectors.domainPreset?.value || "sales",
    install_code: "",
    screen_mode: readScreenMode(),
    modules: collectModules(),
    llm: parseLlmOptionValue(setupSelectors.llmModelOption?.value || ""),
    transcript: {
      backend: setupSelectors.transcriptBackend?.value || "whisper.cpp",
      transcribe_self_live: !!setupSelectors.transcriberSelfLive?.checked,
    },
    prospect_source: setupSelectors.callMedium?.value || "blackhole",
    context_doc_ids: uploadedFiles.map((file) => file.id).filter(Boolean),
    uploads: uploadedFiles,
    // "Server leidt af, dashboard kiest alleen" (klantmap-als-eenheid D2): the only
    // client-related thing this dashboard sends is which client folder was picked (or
    // null, "geen klant"). /api/start-call derives prospect_company, prospect_industry,
    // call_terms and privacy itself from that client's klant.yaml.
    client_slug: setupSelectors.clientSelect?.value || null,
  };
  if (typeof window.getConsentPayload === "function") {
    const consent = window.getConsentPayload();
    if (consent) {
      config.consent = consent;
    }
  }
  return config;
};

const saveConfig = () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(collectConfig()));
  updateDetectionIndicator();
};

const restoreConfig = () => {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) {
    return;
  }
  try {
    const config = JSON.parse(raw);
    if (config.preset_name && setupSelectors.domainPreset) {
      setupSelectors.domainPreset.value = config.preset_name;
    }
    // Same order as applyPreset: modules first, then the screen mode, so the
    // single-screen constraint wins over a stored presentation:true instead of
    // leaving the checkbox ticked but disabled — which collectModules() would
    // still have sent as presentation:true in the start_call payload.
    if (config.modules) {
      writeModuleCheckboxes(config.modules, { deliberate: true });
    }
    if (config.screen_mode) {
      setScreenMode(config.screen_mode);
    }
    applyLlmSelection(config.llm?.provider, config.llm?.model);
    if (config.transcript?.backend && setupSelectors.transcriptBackend) {
      setupSelectors.transcriptBackend.value = config.transcript.backend;
    }
    if (setupSelectors.callMedium) {
      const storedSource = config.prospect_source;
      setupSelectors.callMedium.value =
        storedSource === "audiotee_call" ? "audiotee_call" : "blackhole";
    }
    if (typeof config.transcript?.transcribe_self_live === "boolean" && setupSelectors.transcriberSelfLive) {
      setupSelectors.transcriberSelfLive.checked = config.transcript.transcribe_self_live;
    }
    if (typeof config.client_slug === "string" && config.client_slug) {
      applyClientSelection(config.client_slug);
    }
    if (Array.isArray(config.uploads)) {
      uploadedFiles = config.uploads;
      renderFileList();
    }
  } catch (error) {
    localStorage.removeItem(STORAGE_KEY);
  }
};

const renderFileList = () => {
  if (!setupSelectors.fileList) {
    return;
  }
  setupSelectors.fileList.innerHTML = "";
  uploadedFiles.forEach((file) => {
    const item = document.createElement("div");
    item.className = "document-item";
    const name = document.createElement("span");
    name.textContent = file.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = window.t("setup.remove_file_button");
    remove.addEventListener("click", () => {
      uploadedFiles = uploadedFiles.filter((entry) => entry !== file);
      renderFileList();
      saveConfig();
    });
    item.appendChild(name);
    item.appendChild(remove);
    setupSelectors.fileList.appendChild(item);
  });
};

const uploadFile = async (file) => {
  await window.copilotAuthReady;
  const form = new FormData();
  form.append("file", file);
  form.append("company_slug", slugifyCompany(setupSelectors.clientSelect?.value || ""));
  const apiBaseUrl = resolveApiBaseUrl();
  const uploadUrl = apiBaseUrl ? `${apiBaseUrl}/upload` : "/upload";
  const response = await fetch(uploadUrl, {
    method: "POST",
    headers: { ...window.copilotAuthHeaders() },
    body: form,
  });
  const payload = await safeJson(response);
  if (!response.ok || !payload?.id) {
    throw new Error(payload?.detail || "Upload failed");
  }
  uploadedFiles.push({
    name: payload.name || file.name,
    id: payload.id,
  });
  renderFileList();
  saveConfig();
};

const handleFiles = (files) => {
  Array.from(files).forEach((file) => {
    uploadFile(file).catch(() => {
      if (setupSelectors.setupStatus) {
        setupSelectors.setupStatus.textContent = window.t("setup.upload_failed", { filename: file.name });
      }
    });
  });
};

const initDropzone = () => {
  if (!setupSelectors.dropzone || !setupSelectors.fileInput) {
    return;
  }
  const { dropzone, fileInput } = setupSelectors;

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragover"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragover");
    if (event.dataTransfer?.files) {
      handleFiles(event.dataTransfer.files);
    }
  });
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInput.click();
    }
  });

  fileInput.addEventListener("change", (event) => {
    const files = event.target.files;
    if (files) {
      handleFiles(files);
    }
  });
};

const initScreenToggle = () => {
  if (!setupSelectors.screenToggle) {
    return;
  }
  setupSelectors.screenToggle.querySelectorAll(".toggle-button").forEach((button) => {
    button.addEventListener("click", () => {
      setScreenMode(button.dataset.mode);
      saveConfig();
    });
  });
};

const initFieldListeners = () => {
  [
    setupSelectors.llmModelOption,
    setupSelectors.transcriptBackend,
    setupSelectors.callMedium,
    setupSelectors.domainPreset,
    setupSelectors.clientSelect,
  ].forEach((field) => {
    if (field) {
      field.addEventListener("input", saveConfig);
      field.addEventListener("change", saveConfig);
    }
  });
  setupSelectors.moduleInputs.forEach((input) => {
    input.addEventListener("change", saveConfig);
  });
  if (setupSelectors.transcriberSelfLive) {
    setupSelectors.transcriberSelfLive.addEventListener("change", saveConfig);
  }
};

const sendStartCall = async (config) => {
  await window.copilotAuthReady;
  const apiBaseUrl = resolveApiBaseUrl();
  const url = apiBaseUrl ? `${apiBaseUrl}/api/start-call` : "/api/start-call";
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...window.copilotAuthHeaders() },
    body: JSON.stringify(config),
  });
  if (!response.ok) {
    // The privacy-poort (klantmap-als-eenheid D2) rejects with 400 and a human-readable
    // Dutch `detail` naming the client and the provider tier that failed -- surface that
    // exact reason instead of the generic failure message.
    const payload = await safeJson(response);
    throw new Error(payload?.detail || "Start call failed");
  }
};

const showCallView = () => {
  if (setupSelectors.setupView) {
    setupSelectors.setupView.style.display = "none";
  }
  if (setupSelectors.reportView) {
    setupSelectors.reportView.style.display = "none";
  }
  if (setupSelectors.callView) {
    setupSelectors.callView.style.display = "block";
  }
};

const showSetupView = () => {
  if (setupSelectors.callView) {
    setupSelectors.callView.style.display = "none";
  }
  if (setupSelectors.reportView) {
    setupSelectors.reportView.style.display = "none";
  }
  if (setupSelectors.setupView) {
    setupSelectors.setupView.style.display = "block";
  }
  if (window.resetReportView) {
    window.resetReportView();
  }
  if (window._resetStartCallButton) {
    window._resetStartCallButton();
  }
  if (typeof window.resetConsentState === "function") {
    window.resetConsentState();
  }
};

const showReportView = (report) => {
  if (setupSelectors.setupView) {
    setupSelectors.setupView.style.display = "none";
  }
  if (setupSelectors.callView) {
    setupSelectors.callView.style.display = "none";
  }
  if (setupSelectors.reportView) {
    setupSelectors.reportView.style.display = "block";
  }
  if (window.renderReportView) {
    window.renderReportView(report);
  }
};

const initStartCall = () => {
  if (!setupSelectors.startCall) {
    return;
  }
  const btn = setupSelectors.startCall;
  const originalLabel = btn.textContent;

  window._resetStartCallButton = () => {
    btn.disabled = false;
    btn.textContent = originalLabel;
  };

  btn.addEventListener("click", async () => {
    if (btn.disabled) {
      return;
    }
    const config = collectConfig();
    saveConfig();
    btn.disabled = true;
    btn.textContent = window.t("setup.status_starting");
    if (setupSelectors.setupStatus) {
      setupSelectors.setupStatus.textContent = window.t("setup.status_connecting");
    }
    try {
      await sendStartCall(config);
      // Go through window.showCallView: dashboard-shell.js wraps that property
      // to set body.in-call, which swaps the sidebar Start button for
      // "Stop & rapport". Calling the local binding skips the wrapper, leaving
      // the call with no end-call control and the button stuck on "Starting...".
      window.showCallView();
      if (window.startCallSockets) {
        window.startCallSockets();
      }
      if (setupSelectors.setupStatus) {
        setupSelectors.setupStatus.textContent = window.t("setup.status_in_call");
      }
      // Button stays disabled while in call — re-enabled on showSetupView()
    } catch (error) {
      console.error("Start call failed", error);
      if (setupSelectors.setupStatus) {
        setupSelectors.setupStatus.textContent = window.t("setup.status_connection_failed");
      }
      let message = window.t("setup.start_call_failed_generic");
      if (error?.message?.includes("consent_required") || error?.detail?.includes("consent")) {
        message = window.t("consent.call_blocked_alert");
      } else if (error?.message && error.message !== "Start call failed") {
        // The privacy-poort (klantmap-als-eenheid D2) and klant.yaml validation errors
        // are already human-readable Dutch text from the backend -- show that instead
        // of the generic fallback.
        message = error.message;
      }
      window.alert(message);
      // 2s lockout before re-enabling after error
      setTimeout(() => {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }, 2000);
    }
  });
};

window.showCallView = showCallView;
window.showSetupView = showSetupView;
window.showReportView = showReportView;

restoreConfig();
updateDetectionIndicator();
loadLlmModelOptions().then(() => {
  restoreConfig();
  if (presets[0]) {
    applyPreset(presets[0], { deliberate: false });
  }
});
loadClients().then(() => {
  restoreConfig();
});
loadPresets();
initScreenToggle();
initDropzone();
initFieldListeners();
initStartCall();
window.SalesCopilotI18n.ready.then(() => {
  if (uploadedFiles.length) {
    renderFileList();
  }
});
})();
