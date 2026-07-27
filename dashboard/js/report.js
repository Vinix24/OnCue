(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getWsBaseUrl = () => window.WS_BASE_URL || DEFAULT_WS_BASE_URL;
const getReportWsUrl = () => `${getWsBaseUrl()}/ws/coaching`;
const getConfigWsUrl = () => `${getWsBaseUrl()}/ws/config`;

const reportSelectors = {
  endCallButton: document.getElementById("end-call"),
  view: document.getElementById("report-view"),
  duration: document.getElementById("report-duration"),
  selfPct: document.getElementById("report-self"),
  prospectPct: document.getElementById("report-prospect"),
  monologues: document.getElementById("report-monologues"),
  scorecardBlock: document.getElementById("report-scorecard-block"),
  scorecard: document.getElementById("report-scorecard"),
  painPoints: document.getElementById("report-pain-points"),
  objections: document.getElementById("report-objections"),
  keyMoments: document.getElementById("report-key-moments"),
  finalSummary: document.getElementById("report-final-summary"),
  downloadJson: document.getElementById("report-download-json"),
  downloadMarkdown: document.getElementById("report-download-markdown"),
  newCall: document.getElementById("report-new-call"),
};

let latestReport = null;
let reportSocket = null;
let configSocket = null;
let endCallLocked = false;

const emptySummaryText = () => window.t("summary.empty_state");

const formatDuration = (ms) => {
  if (typeof ms !== "number" || Number.isNaN(ms)) {
    return "00:00";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const two = (num) => String(num).padStart(2, "0");
  return hours > 0 ? `${two(hours)}:${two(minutes)}:${two(seconds)}` : `${two(minutes)}:${two(seconds)}`;
};

const toPercent = (value) => {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return 0;
  }
  const normalized = value <= 1 ? value * 100 : value;
  return Math.round(normalized);
};

const formatTimestamp = (ms) => {
  if (typeof ms !== "number" || Number.isNaN(ms)) {
    return "--:--";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
};

const escapeMarkdown = (value) => String(value || "").replace(/[\\`*_{}\[\]()#+\-.!|]/g, "\\$&");

const setListItems = (container, items, emptyText) => {
  if (!container) {
    return;
  }
  container.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "report-empty";
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "report-row";
    const title = document.createElement("strong");
    title.textContent = item.title || window.t("common.category_unknown");
    const meta = document.createElement("span");
    meta.textContent = item.meta || "--:--";
    row.appendChild(title);
    row.appendChild(meta);
    if (item.detail) {
      const detail = document.createElement("p");
      detail.className = "report-row-detail";
      detail.textContent = item.detail;
      row.appendChild(detail);
    }
    container.appendChild(row);
  });
};

const painPointsFromReport = (report) => {
  const entries = Array.isArray(report?.pain_points_detected) ? report.pain_points_detected : [];
  return entries.map((item) => ({
    title: item?.category || window.t("common.category_unknown"),
    meta: `${formatTimestamp(item?.timestamp_ms)}${item?.case_id ? ` · ${item.case_id}` : ""}`,
    detail: item?.trigger_phrase || "",
  }));
};

const objectionsFromDom = () => {
  const panel = document.getElementById("objections-panel");
  if (!panel) {
    return [];
  }
  const cards = Array.from(panel.querySelectorAll(".pain-point-card"));
  return cards.map((card) => {
    const title = card.querySelector(".pain-point-badge")?.textContent?.trim() || window.t("objections.fallback_label");
    const timestamp = card.querySelector(".pain-point-meta span:last-child")?.textContent?.trim() || "--:--";
    const detail = card.querySelector(".pain-point-case")?.textContent?.trim() || "";
    return {
      title,
      meta: timestamp,
      detail,
    };
  });
};

const objectionsFromReport = (report) => {
  if (Array.isArray(report?.objections)) {
    return report.objections.map((item) => ({
      title: item?.category || window.t("objections.fallback_label"),
      meta: formatTimestamp(item?.timestamp_ms),
      detail: item?.response_suggestion || "",
    }));
  }
  const keyMoments = Array.isArray(report?.key_moments) ? report.key_moments : [];
  const objections = keyMoments
    .filter((item) => String(item?.type || "").toLowerCase() === "objection")
    .map((item) => ({
      title: item?.description || window.t("objections.fallback_label"),
      meta: formatTimestamp(item?.timestamp_ms),
      detail: "",
    }));
  return objections.length ? objections : objectionsFromDom();
};

const keyMomentsFromReport = (report) => {
  const entries = Array.isArray(report?.key_moments) ? report.key_moments : [];
  return entries.map((item) => ({
    title: item?.type || window.t("report.key_moment_fallback_label"),
    meta: formatTimestamp(item?.timestamp_ms),
    detail: item?.description || "",
  }));
};

// Phase 3 Free post-call scorecard: the count and the gap, deterministic and
// local. Copy says "gedetecteerd", never "gegarandeerd". Hiding this section
// leaves all data stored on the session (the phase's rollback invariant).
const scorecardFromReport = (report) => {
  const scorecard = report?.scorecard;
  if (!scorecard || typeof scorecard !== "object") {
    return [];
  }
  const items = [];
  if (typeof scorecard.script_total === "number") {
    const covered = typeof scorecard.script_covered === "number" ? scorecard.script_covered : 0;
    const missing = Array.isArray(scorecard.missing_required) ? scorecard.missing_required : [];
    items.push({
      title: window.t("report.scorecard_script_coverage", { covered, total: scorecard.script_total }),
      meta: `${covered}/${scorecard.script_total}`,
      detail: missing.length
        ? window.t("report.scorecard_missing_points", { points: missing.join(", ") })
        : window.t("report.scorecard_no_missing_points"),
    });
  }
  const objectionCount = typeof scorecard.objection_count === "number" ? scorecard.objection_count : 0;
  const respondedCount =
    typeof scorecard.objection_responded_count === "number" ? scorecard.objection_responded_count : 0;
  items.push({
    title: window.t("report.scorecard_objections_responded", { count: objectionCount, responded: respondedCount }),
    meta: `${respondedCount}/${objectionCount}`,
    detail: "",
  });
  const opportunityCount =
    typeof scorecard.opportunity_count === "number" ? scorecard.opportunity_count : 0;
  items.push({
    title: window.t("report.scorecard_opportunities_detected", { count: opportunityCount }),
    meta: String(opportunityCount),
    detail: "",
  });
  return items;
};

const applyScorecard = (report) => {
  const items = scorecardFromReport(report);
  if (!reportSelectors.scorecardBlock) {
    return;
  }
  if (!items.length) {
    reportSelectors.scorecardBlock.style.display = "none";
    return;
  }
  reportSelectors.scorecardBlock.style.display = "";
  setListItems(reportSelectors.scorecard, items, window.t("report.no_scorecard"));
};

const summaryFromReport = (report) => {
  if (typeof report?.conversation_summary === "string" && report.conversation_summary.trim()) {
    return report.conversation_summary.trim();
  }
  const summaryText = document.getElementById("summary-text");
  if (summaryText && summaryText.textContent && summaryText.textContent.trim()) {
    return summaryText.textContent.trim();
  }
  return emptySummaryText();
};

const ensureReport = (report) => {
  const base = report && typeof report === "object" ? report : {};
  return {
    ...base,
    call_duration_ms: base.call_duration_ms ?? 0,
    total_self_pct: base.total_self_pct ?? 0,
    total_prospect_pct: base.total_prospect_pct ?? 0,
    monologue_count: base.monologue_count ?? 0,
    pain_points_detected: Array.isArray(base.pain_points_detected) ? base.pain_points_detected : [],
    key_moments: Array.isArray(base.key_moments) ? base.key_moments : [],
    conversation_summary: summaryFromReport(base),
    objections: objectionsFromReport(base),
  };
};

const applyReport = (report) => {
  latestReport = ensureReport(report);
  if (reportSelectors.duration) {
    reportSelectors.duration.textContent = formatDuration(latestReport.call_duration_ms);
  }
  if (reportSelectors.selfPct) {
    reportSelectors.selfPct.textContent = `${toPercent(latestReport.total_self_pct)}%`;
  }
  if (reportSelectors.prospectPct) {
    reportSelectors.prospectPct.textContent = `${toPercent(latestReport.total_prospect_pct)}%`;
  }
  if (reportSelectors.monologues) {
    reportSelectors.monologues.textContent = String(latestReport.monologue_count ?? 0);
  }
  if (reportSelectors.finalSummary) {
    reportSelectors.finalSummary.textContent = latestReport.conversation_summary || emptySummaryText();
  }
  setListItems(reportSelectors.painPoints, painPointsFromReport(latestReport), window.t("report.no_pain_points"));
  setListItems(reportSelectors.objections, latestReport.objections || [], window.t("report.no_objections"));
  setListItems(reportSelectors.keyMoments, keyMomentsFromReport(latestReport), window.t("report.no_key_moments"));
  applyScorecard(latestReport);
};

const downloadBlob = (content, mimeType, extension) => {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const timestamp = new Date().toISOString().replace(/:/g, "-");
  link.href = url;
  link.download = `${timestamp}_report.${extension}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
};

const downloadReportJson = () => {
  if (!latestReport) {
    return;
  }
  downloadBlob(JSON.stringify(latestReport, null, 2), "application/json", "json");
};

const reportToMarkdown = (report) => {
  const painPoints = painPointsFromReport(report);
  const objections = report.objections || [];
  const keyMoments = keyMomentsFromReport(report);
  const scorecard = scorecardFromReport(report);
  const none = window.t("report.markdown_none");
  const lines = [
    `# ${window.t("report.title")}`,
    "",
    `- ${window.t("report.call_duration_label")}: ${formatDuration(report.call_duration_ms)}`,
    `- ${window.t("report.talktime_self_prospect_label")}: ${toPercent(report.total_self_pct)}% / ${toPercent(report.total_prospect_pct)}%`,
    `- ${window.t("report.monologue_warnings_label")}: ${report.monologue_count ?? 0}`,
    "",
  ];
  if (scorecard.length) {
    lines.push(`## ${window.t("report.scorecard_title")}`, "");
    scorecard.forEach((item) => {
      lines.push(`- ${escapeMarkdown(item.title)}`);
      if (item.detail) {
        lines.push(`  - ${escapeMarkdown(item.detail)}`);
      }
    });
    lines.push("");
  }
  lines.push(`## ${window.t("painpoints.title")}`, "");
  if (!painPoints.length) {
    lines.push(`- ${none}`);
  } else {
    painPoints.forEach((item) => {
      lines.push(`- ${escapeMarkdown(item.meta)} - ${escapeMarkdown(item.title)}`);
      if (item.detail) {
        lines.push(`  - ${escapeMarkdown(item.detail)}`);
      }
    });
  }
  lines.push("", `## ${window.t("objections.title")}`, "");
  if (!objections.length) {
    lines.push(`- ${none}`);
  } else {
    objections.forEach((item) => {
      lines.push(`- ${escapeMarkdown(item.meta)} - ${escapeMarkdown(item.title)}`);
      if (item.detail) {
        lines.push(`  - ${escapeMarkdown(item.detail)}`);
      }
    });
  }
  lines.push("", `## ${window.t("report.key_moments_title")}`, "");
  if (!keyMoments.length) {
    lines.push(`- ${none}`);
  } else {
    keyMoments.forEach((item) => {
      lines.push(`- ${escapeMarkdown(item.meta)} - ${escapeMarkdown(item.title)}`);
      if (item.detail) {
        lines.push(`  - ${escapeMarkdown(item.detail)}`);
      }
    });
  }
  lines.push("", `## ${window.t("report.final_summary_title")}`, "", report.conversation_summary || emptySummaryText());
  return lines.join("\n");
};

const downloadReportMarkdown = () => {
  if (!latestReport) {
    return;
  }
  downloadBlob(reportToMarkdown(latestReport), "text/markdown", "md");
};

const showReportFromCall = () => {
  if (window.stopCallSockets) {
    window.stopCallSockets();
  }
  if (window.showReportView) {
    window.showReportView(latestReport);
  }
};

const sendEndCall = async () => {
  await window.copilotAuthReady;
  const response = await fetch("/api/end-call", {
    method: "POST",
    headers: { ...window.copilotAuthHeaders() },
  });
  if (!response.ok) {
    throw new Error("End call failed");
  }
};

const connectReportSocket = () => {
  const socket = new WebSocket(getReportWsUrl());
  reportSocket = socket;

  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (
        payload &&
        (payload.type === "report_ready" || payload.event === "report_ready" || payload.action === "report_ready")
      ) {
        applyReport(payload.report || payload.data || payload);
      }
    } catch (error) {
      console.error("Invalid report message", error);
    }
  });

  socket.addEventListener("close", () => {
    setTimeout(connectReportSocket, 3000);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });

};

const resetEndCallButton = () => {
  endCallLocked = false;
  if (reportSelectors.endCallButton) {
    reportSelectors.endCallButton.disabled = false;
  }
};

const connectConfigSocket = () => {
  const socket = new WebSocket(getConfigWsUrl());
  configSocket = socket;

  socket.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload?.type === "call_ended") {
        resetEndCallButton();
        showReportFromCall();
        return;
      }
      if (payload?.type === "start_call") {
        if (window.resetCallPanels) {
          window.resetCallPanels();
        }
      }
    } catch (error) {
      console.error("Invalid config message", error);
    }
  });

  socket.addEventListener("close", () => {
    setTimeout(connectConfigSocket, 3000);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });
};

if (reportSelectors.downloadJson) {
  reportSelectors.downloadJson.addEventListener("click", downloadReportJson);
}

if (reportSelectors.downloadMarkdown) {
  reportSelectors.downloadMarkdown.addEventListener("click", downloadReportMarkdown);
}

if (reportSelectors.endCallButton) {
  reportSelectors.endCallButton.addEventListener("click", () => {
    if (endCallLocked) {
      return;
    }
    endCallLocked = true;
    reportSelectors.endCallButton.disabled = true;
    sendEndCall().catch((error) => {
      console.error(error);
      resetEndCallButton();
    });
    // Safety re-enable after 5s in case call_ended event doesn't arrive
    setTimeout(() => {
      if (endCallLocked) {
        resetEndCallButton();
      }
    }, 5000);
  });
}

if (reportSelectors.newCall) {
  reportSelectors.newCall.addEventListener("click", () => {
    if (window.showSetupView) {
      window.showSetupView();
    }
  });
}

window.renderReportView = (report) => {
  applyReport(report || latestReport);
};

window.resetReportView = () => {
  latestReport = null;
  if (reportSelectors.duration) {
    reportSelectors.duration.textContent = "00:00";
  }
  if (reportSelectors.selfPct) {
    reportSelectors.selfPct.textContent = "0%";
  }
  if (reportSelectors.prospectPct) {
    reportSelectors.prospectPct.textContent = "0%";
  }
  if (reportSelectors.monologues) {
    reportSelectors.monologues.textContent = "0";
  }
  if (reportSelectors.finalSummary) {
    reportSelectors.finalSummary.textContent = emptySummaryText();
  }
  setListItems(reportSelectors.painPoints, [], window.t("report.no_pain_points"));
  setListItems(reportSelectors.objections, [], window.t("report.no_objections"));
  setListItems(reportSelectors.keyMoments, [], window.t("report.no_key_moments"));
  setListItems(reportSelectors.scorecard, [], window.t("report.no_scorecard"));
  if (reportSelectors.scorecardBlock) {
    reportSelectors.scorecardBlock.style.display = "none";
  }
};

window.SalesCopilotI18n.ready.then(() => window.resetReportView());
connectReportSocket();
connectConfigSocket();
})();
