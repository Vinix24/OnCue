(() => {
const PRESET_ENDPOINT = "/api/presets";
const LLM_MODELS_ENDPOINT = "/api/llm-models";
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
  prospectCompany: document.getElementById("prospect-company"),
  prospectIndustry: document.getElementById("prospect-industry"),
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
    applyPreset(presets[0]);
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

const setScreenMode = (mode) => {
  if (!setupSelectors.screenToggle) {
    return;
  }
  setupSelectors.screenToggle.querySelectorAll(".toggle-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  const presentationToggle = setupSelectors.moduleInputs.find((input) => input.dataset.module === "presentation");
  if (presentationToggle) {
    if (mode === "single") {
      presentationToggle.checked = false;
      presentationToggle.disabled = true;
    } else {
      presentationToggle.disabled = false;
    }
  }
};

const applyPreset = (preset) => {
  if (!preset) {
    return;
  }
  if (preset.screen_mode) {
    setScreenMode(preset.screen_mode);
  }
  if (preset.modules && setupSelectors.moduleInputs.length) {
    setupSelectors.moduleInputs.forEach((input) => {
      const value = preset.modules[input.dataset.module];
      if (typeof value === "boolean") {
        input.checked = value;
      }
    });
  }
  applyLlmSelection(preset.llm_provider, preset.llm_model);
  if (setupSelectors.transcriptBackend && preset.transcript?.backend) {
    setupSelectors.transcriptBackend.value = preset.transcript.backend;
  }
  if (setupSelectors.transcriberSelfLive && typeof preset.transcript?.transcribe_self_live === "boolean") {
    setupSelectors.transcriberSelfLive.checked = preset.transcript.transcribe_self_live;
  }
  if (setupSelectors.prospectCompany && preset.prospect_company) {
    setupSelectors.prospectCompany.value = preset.prospect_company;
  }
  if (setupSelectors.prospectIndustry && preset.prospect_industry) {
    setupSelectors.prospectIndustry.value = preset.prospect_industry;
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
    prospect: {
      company: setupSelectors.prospectCompany?.value || "",
      industry: setupSelectors.prospectIndustry?.value || "",
    },
    prospect_source: setupSelectors.callMedium?.value || "blackhole",
    context_doc_ids: uploadedFiles.map((file) => file.id).filter(Boolean),
    uploads: uploadedFiles,
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
};

const restoreConfig = () => {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) {
    return;
  }
  try {
    const config = JSON.parse(raw);
    if (config.screen_mode) {
      setScreenMode(config.screen_mode);
    }
    if (config.preset_name && setupSelectors.domainPreset) {
      setupSelectors.domainPreset.value = config.preset_name;
    }
    if (config.modules) {
      setupSelectors.moduleInputs.forEach((input) => {
        // pain_points is the AI-detection master switch — always force true
        // on restore so a stale "unchecked" cache cannot silently disable
        // pain points / objections / suggestions detection.
        if (input.dataset.module === "pain_points") {
          input.checked = true;
          return;
        }
        if (typeof config.modules[input.dataset.module] === "boolean") {
          input.checked = config.modules[input.dataset.module];
        }
      });
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
    if (config.prospect?.company && setupSelectors.prospectCompany) {
      setupSelectors.prospectCompany.value = config.prospect.company;
    }
    if (config.prospect?.industry && setupSelectors.prospectIndustry) {
      setupSelectors.prospectIndustry.value = config.prospect.industry;
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
  form.append("company_slug", slugifyCompany(setupSelectors.prospectCompany?.value || ""));
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
    setupSelectors.prospectCompany,
    setupSelectors.prospectIndustry,
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
    throw new Error("Start call failed");
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
      showCallView();
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
loadLlmModelOptions().then(() => {
  restoreConfig();
  if (presets[0]) {
    applyPreset(presets[0]);
  }
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
