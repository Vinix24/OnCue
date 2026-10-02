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
  insights: document.getElementById("report-insights"),
  finalSummary: document.getElementById("report-final-summary"),
  junkBanner: document.getElementById("report-junk-banner"),
  junkReason: document.getElementById("report-junk-reason"),
  termCorrectionsBlock: document.getElementById("report-term-corrections-block"),
  termCorrections: document.getElementById("report-term-corrections"),
  termCorrectionsNote: document.getElementById("report-term-corrections-note"),
  downloadJson: document.getElementById("report-download-json"),
  downloadMarkdown: document.getElementById("report-download-markdown"),
  newCall: document.getElementById("report-new-call"),
  status: document.getElementById("report-status"),
  enrichmentBlock: document.getElementById("report-enrichment-block"),
  shortSummary: document.getElementById("report-short-summary"),
  overview: document.getElementById("report-overview"),
  keywords: document.getElementById("report-keywords"),
  actionItems: document.getElementById("report-action-items"),
};

// The enrichment runs after the call and can take a minute or more. When report_enriched has not
// arrived by then, the status line says the report is saved locally instead of promising more.
const ENRICHMENT_WAIT_MS = 6 * 60 * 1000;

let latestReport = null;
let reportSocket = null;
let configSocket = null;
let endCallLocked = false;
// Bumped on every new report and reset, so a timer or fetch of an earlier report does nothing.
let enrichmentRun = 0;

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

// Track 3 (deep insight lane): one row per `InsightEntry` from the persisted
// post-call report. `antwoord` rows carry the vraag-box question that
// triggered them; folded into the detail line so the export stays flat.
const insightsFromReport = (report) => {
  const entries = Array.isArray(report?.insights) ? report.insights : [];
  return entries.map((item) => ({
    title: window.t(`insights.type_${item?.insight_type}`) || item?.insight_type || window.t("report.key_moment_fallback_label"),
    meta: formatTimestamp(item?.timestamp_ms),
    detail: item?.question
      ? `${item?.text || ""} (${window.t("insights.question_label")}: ${item.question})`
      : item?.text || "",
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

// Term correction (termenlijst-in-uitwerking D2): the transcript keeps the original, each
// correction says where a list term was meant. The list only comes with a full report; the
// report_enriched event carries the counts and never the text (no PII on that channel).
const termCorrectionsFromReport = (report) =>
  Array.isArray(report?.term_corrections) ? report.term_corrections : [];

const termCorrectionCount = (report) =>
  typeof report?.term_correction_count === "number"
    ? report.term_correction_count
    : termCorrectionsFromReport(report).length;

const termCorrectionsPiiLimited = (report) =>
  typeof report?.term_corrections_pii_limited === "number" ? report.term_corrections_pii_limited : 0;

const termCorrectionMeta = (report, item) => {
  const segment = Array.isArray(report?.full_transcript) ? report.full_transcript[item?.segment_index] : null;
  if (typeof segment?.start_ms === "number") {
    return formatTimestamp(segment.start_ms);
  }
  return window.t("report.term_corrections_segment", { index: item?.segment_index ?? "?" });
};

// The notes under the list: the count when only the count arrived, and the PII limit.
const termCorrectionNotes = (report) => {
  const notes = [];
  const count = termCorrectionCount(report);
  if (!termCorrectionsFromReport(report).length && count > 0) {
    notes.push(window.t("report.term_corrections_saved_count", { count }));
  }
  const piiLimited = termCorrectionsPiiLimited(report);
  if (piiLimited > 0) {
    notes.push(window.t("report.term_corrections_pii_limited", { count: piiLimited }));
  }
  return notes;
};

const termCorrectionPart = (className, text) => {
  const part = document.createElement("span");
  part.className = className;
  part.textContent = text;
  return part;
};

const termCorrectionRow = (report, item) => {
  const row = document.createElement("div");
  row.className = "report-row term-correction-row";
  const pair = document.createElement("strong");
  pair.className = "term-correction-pair";
  pair.appendChild(termCorrectionPart("term-correction-label", window.t("report.term_corrections_original_label")));
  pair.appendChild(termCorrectionPart("term-correction-original", item?.source || ""));
  pair.appendChild(termCorrectionPart("term-correction-arrow", "→"));
  pair.appendChild(termCorrectionPart("term-correction-label", window.t("report.term_corrections_corrected_label")));
  pair.appendChild(termCorrectionPart("term-correction-target", item?.target || ""));
  const meta = document.createElement("span");
  meta.textContent = termCorrectionMeta(report, item);
  row.appendChild(pair);
  row.appendChild(meta);
  if (item?.reason) {
    const detail = document.createElement("p");
    detail.className = "report-row-detail";
    detail.textContent = item.reason;
    row.appendChild(detail);
  }
  return row;
};

const applyTermCorrections = (report) => {
  const { termCorrectionsBlock: block, termCorrections: list, termCorrectionsNote: note } = reportSelectors;
  if (!block) {
    return;
  }
  const items = termCorrectionsFromReport(report);
  const notes = termCorrectionNotes(report);
  if (list) {
    list.textContent = "";
    items.forEach((item) => list.appendChild(termCorrectionRow(report, item)));
  }
  if (note) {
    note.textContent = notes.join(" ");
  }
  block.style.display = items.length || notes.length ? "" : "none";
};

const textList = (value) =>
  Array.isArray(value) ? value.filter((item) => typeof item === "string" && item.trim()) : [];

const enrichmentText = (report) => ({
  shortSummary: typeof report?.short_summary === "string" ? report.short_summary.trim() : "",
  overview: typeof report?.overview === "string" ? report.overview.trim() : "",
  keywords: textList(report?.keywords),
  actionItems: textList(report?.action_items),
});

const applyEnrichment = (report) => {
  const block = reportSelectors.enrichmentBlock;
  if (!block) {
    return;
  }
  const text = enrichmentText(report);
  if (reportSelectors.shortSummary) {
    reportSelectors.shortSummary.textContent = text.shortSummary;
  }
  if (reportSelectors.overview) {
    reportSelectors.overview.textContent = text.overview;
  }
  if (reportSelectors.keywords) {
    reportSelectors.keywords.textContent = text.keywords.join(", ");
  }
  setListItems(
    reportSelectors.actionItems,
    text.actionItems.map((item) => ({ title: item, meta: "" })),
    "",
  );
  const hasContent = Boolean(text.shortSummary || text.overview || text.keywords.length || text.actionItems.length);
  block.style.display = hasContent ? "" : "none";
};

let statusKey = null;

const setStatus = (key) => {
  statusKey = key;
  const status = reportSelectors.status;
  if (!status) {
    return;
  }
  status.textContent = key ? window.t(key) : "";
  status.style.display = key ? "" : "none";
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
    insights: Array.isArray(base.insights) ? base.insights : [],
    conversation_summary: summaryFromReport(base),
    objections: objectionsFromReport(base),
  };
};

// Reports written before the junk filter carry no junk keys: they read as not junk.
const applyJunkBanner = (report) => {
  if (!reportSelectors.junkBanner) {
    return;
  }
  const isJunk = Boolean(report && report.junk);
  reportSelectors.junkBanner.style.display = isJunk ? "" : "none";
  if (reportSelectors.junkReason) {
    reportSelectors.junkReason.textContent = isJunk ? report.junk_reason || "" : "";
  }
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
  setListItems(reportSelectors.insights, insightsFromReport(latestReport), window.t("report.no_insights"));
  applyScorecard(latestReport);
  applyJunkBanner(latestReport);
  applyTermCorrections(latestReport);
  applyEnrichment(latestReport);
};

// report_ready goes out before the post-call enrichment; report_enriched follows with the
// junk decision and the term-correction counts.
const startWaitingForEnrichment = () => {
  enrichmentRun += 1;
  const run = enrichmentRun;
  setStatus("report.status_preparing");
  setTimeout(() => {
    if (run === enrichmentRun && statusKey === "report.status_preparing") {
      setStatus("report.status_local_only");
    }
  }, ENRICHMENT_WAIT_MS);
};

// The enrichment text never rides on the event: it is read from the local report through the
// token-protected endpoint, once report_enriched says it is there.
const fetchEnrichedReport = async (sessionId, run) => {
  await window.copilotAuthReady;
  const response = await fetch(`/api/reports/${encodeURIComponent(sessionId)}`, {
    headers: { ...window.copilotAuthHeaders() },
  });
  if (!response.ok) {
    throw new Error(`Report fetch failed: ${response.status}`);
  }
  const stored = await response.json();
  if (run !== enrichmentRun || !latestReport || latestReport.session_id !== sessionId) {
    return;
  }
  latestReport = {
    ...latestReport,
    gesprek_gevoerd: stored.gesprek_gevoerd,
    short_summary: stored.short_summary,
    overview: stored.overview,
    keywords: stored.keywords,
    action_items: stored.action_items,
    term_corrections: stored.term_corrections,
    term_corrections_pii_limited: stored.term_corrections_pii_limited,
  };
  applyEnrichment(latestReport);
  applyTermCorrections(latestReport);
  setStatus(null);
};

const applyReportEnriched = (payload) => {
  if (!latestReport || (latestReport.session_id && payload.session_id !== latestReport.session_id)) {
    return;
  }
  latestReport = {
    ...latestReport,
    gesprek_gevoerd: payload.gesprek_gevoerd,
    junk: Boolean(payload.junk),
    junk_reason: payload.junk_reason || null,
    term_correction_count: typeof payload.term_correction_count === "number" ? payload.term_correction_count : 0,
    term_corrections_pii_limited:
      typeof payload.term_corrections_pii_limited === "number" ? payload.term_corrections_pii_limited : 0,
  };
  applyJunkBanner(latestReport);
  applyTermCorrections(latestReport);
  if (!latestReport.session_id) {
    setStatus(null);
    return;
  }
  const run = enrichmentRun;
  fetchEnrichedReport(latestReport.session_id, run).catch((error) => {
    console.error(error);
    if (run === enrichmentRun) {
      setStatus("report.status_fetch_failed");
    }
  });
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
  const insights = insightsFromReport(report);
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
  lines.push("", `## ${window.t("report.insights_title")}`, "");
  if (!insights.length) {
    lines.push(`- ${none}`);
  } else {
    insights.forEach((item) => {
      lines.push(`- ${escapeMarkdown(item.meta)} - ${escapeMarkdown(item.title)}`);
      if (item.detail) {
        lines.push(`  - ${escapeMarkdown(item.detail)}`);
      }
    });
  }
  const enrichment = enrichmentText(report);
  if (enrichment.shortSummary) {
    lines.push("", `## ${window.t("report.short_summary_title")}`, "", escapeMarkdown(enrichment.shortSummary));
  }
  if (enrichment.overview) {
    lines.push("", `## ${window.t("report.overview_title")}`, "", escapeMarkdown(enrichment.overview));
  }
  if (enrichment.keywords.length) {
    lines.push("", `## ${window.t("report.keywords_title")}`, "", escapeMarkdown(enrichment.keywords.join(", ")));
  }
  if (enrichment.actionItems.length) {
    lines.push("", `## ${window.t("report.action_items_title")}`, "");
    enrichment.actionItems.forEach((item) => lines.push(`- ${escapeMarkdown(item)}`));
  }
  const termCorrections = termCorrectionsFromReport(report);
  const termNotes = termCorrectionNotes(report);
  if (termCorrections.length || termNotes.length) {
    lines.push("", `## ${window.t("report.term_corrections_title")}`, "");
    termCorrections.forEach((item) => {
      lines.push(
        `- ${escapeMarkdown(termCorrectionMeta(report, item))} - ` +
          `${escapeMarkdown(window.t("report.term_corrections_original_label"))}: ${escapeMarkdown(item?.source)} → ` +
          `${escapeMarkdown(window.t("report.term_corrections_corrected_label"))}: ${escapeMarkdown(item?.target)}`,
      );
      if (item?.reason) {
        lines.push(`  - ${escapeMarkdown(item.reason)}`);
      }
    });
    termNotes.forEach((text) => lines.push(`- ${escapeMarkdown(text)}`));
  }
  if (report.junk) {
    lines.push("", `> ${window.t("report.junk_title")}: ${escapeMarkdown(report.junk_reason || "")}`);
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
        startWaitingForEnrichment();
      } else if (payload && payload.type === "report_enriched") {
        applyReportEnriched(payload);
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
  enrichmentRun += 1;
  setStatus(null);
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
  setListItems(reportSelectors.insights, [], window.t("report.no_insights"));
  setListItems(reportSelectors.scorecard, [], window.t("report.no_scorecard"));
  applyJunkBanner(null);
  applyTermCorrections(null);
  applyEnrichment(null);
  if (reportSelectors.scorecardBlock) {
    reportSelectors.scorecardBlock.style.display = "none";
  }
};

window.SalesCopilotI18n.ready.then(() => window.resetReportView());
connectReportSocket();
connectConfigSocket();
})();
