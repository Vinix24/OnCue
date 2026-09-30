(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getTranscriptWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/transcript`;
const transcriptPanel = document.getElementById("transcript-list");
const MAX_ENTRIES = 200;
const SCROLL_THRESHOLD = 24;

if (transcriptPanel) {
  let shouldAutoScroll = true;
  let socket;
  let retryCount = 0;

  // Track active partial rows by "${speaker}_${start_ms}" key
  const _partialRows = new Map();

  const _partialKey = (payload) =>
    `${payload.speaker}_${typeof payload.start_ms === "number" ? payload.start_ms : 0}`;

  const formatTimestamp = (ms) => {
    if (typeof ms !== "number" || Number.isNaN(ms)) {
      return "--:--";
    }
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  };

  const updateAutoScroll = () => {
    const distanceFromBottom =
      transcriptPanel.scrollHeight - transcriptPanel.scrollTop - transcriptPanel.clientHeight;
    shouldAutoScroll = distanceFromBottom <= SCROLL_THRESHOLD;
    transcriptPanel.classList.toggle("paused", !shouldAutoScroll);
  };

  const scrollToBottom = () => {
    transcriptPanel.scrollTop = transcriptPanel.scrollHeight;
  };

  const pruneEntries = () => {
    while (transcriptPanel.children.length > MAX_ENTRIES) {
      transcriptPanel.removeChild(transcriptPanel.firstChild);
    }
  };

  const _createRow = (payload, startMs) => {
    const row = document.createElement("div");
    row.className = "transcript-row";
    row.dataset.startMs = String(startMs);

    const speaker = document.createElement("span");
    speaker.className = `transcript-speaker ${payload.speaker === "prospect" ? "prospect" : "self"}`;
    speaker.textContent = payload.speaker === "prospect"
      ? window.t("talktime.prospect_label")
      : window.t("talktime.you_label");
    speaker.dataset.role = payload.speaker === "prospect" ? "prospect" : "self";

    const text = document.createElement("span");
    text.className = "transcript-text";
    text.textContent = payload.text || "";

    const time = document.createElement("span");
    time.className = "transcript-time";
    time.textContent = formatTimestamp(payload.start_ms);

    row.appendChild(speaker);
    row.appendChild(text);
    row.appendChild(time);
    return row;
  };

  const _insertRow = (row, startMs) => {
    // Insertion-sort by start_ms so late-arriving self entries (shared-queue
    // priority architecture) appear in chronological order between prospect
    // entries instead of clumping at the end.
    const rows = transcriptPanel.children;
    let inserted = false;
    for (let i = rows.length - 1; i >= 0; i--) {
      const existing = Number(rows[i].dataset.startMs || 0);
      if (existing <= startMs) {
        if (i === rows.length - 1) {
          transcriptPanel.appendChild(row);
        } else {
          transcriptPanel.insertBefore(row, rows[i + 1]);
        }
        inserted = true;
        break;
      }
    }
    if (!inserted) {
      transcriptPanel.insertBefore(row, transcriptPanel.firstChild);
    }
  };

  const handlePartialEntry = (payload) => {
    const key = _partialKey(payload);
    if (_partialRows.has(key)) {
      const row = _partialRows.get(key);
      const textSpan = row.querySelector(".transcript-text");
      if (textSpan) textSpan.textContent = payload.text || "";
      if (shouldAutoScroll) scrollToBottom();
      return;
    }
    const startMs = typeof payload.start_ms === "number" ? payload.start_ms : 0;
    const row = _createRow(payload, startMs);
    row.classList.add("transcript-partial");
    _insertRow(row, startMs);
    _partialRows.set(key, row);
    pruneEntries();
    if (shouldAutoScroll) scrollToBottom();
  };

  // "The audio stopped here", inline and in place. A warning in the coaching
  // panel scrolls away; this row stays where the gap is, so reading back never
  // suggests the speaker went quiet.
  const addMarker = (payload) => {
    if (!payload || typeof payload.text !== "string" || !payload.text.trim()) {
      return;
    }
    const startMs = typeof payload.start_ms === "number" ? payload.start_ms : 0;
    const row = document.createElement("div");
    row.className = "transcript-row transcript-marker";
    row.dataset.startMs = String(startMs);

    const speaker = document.createElement("span");
    speaker.className = "transcript-speaker marker";
    speaker.textContent = window.t("transcript.marker_label");

    const text = document.createElement("span");
    text.className = "transcript-text";
    text.textContent = payload.text;

    const time = document.createElement("span");
    time.className = "transcript-time";
    time.textContent = formatTimestamp(startMs);

    row.appendChild(speaker);
    row.appendChild(text);
    row.appendChild(time);
    _insertRow(row, startMs);
    pruneEntries();
    if (shouldAutoScroll) scrollToBottom();
  };

  const addEntry = (payload) => {
    if (!payload || payload.type !== "transcript") {
      return;
    }

    const startMs = typeof payload.start_ms === "number" ? payload.start_ms : 0;
    const key = _partialKey(payload);

    if (_partialRows.has(key)) {
      const row = _partialRows.get(key);
      row.classList.remove("transcript-partial");
      const textSpan = row.querySelector(".transcript-text");
      if (textSpan) textSpan.textContent = payload.text || "";
      _partialRows.delete(key);
      if (shouldAutoScroll) scrollToBottom();
      return;
    }

    const row = _createRow(payload, startMs);
    _insertRow(row, startMs);

    if (shouldAutoScroll) scrollToBottom();
    pruneEntries();
  };

  const connect = () => {
    socket = new WebSocket(getTranscriptWsUrl());

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload && payload.type === "partial_transcript") {
          handlePartialEntry(payload);
        } else if (payload && payload.type === "transcript_marker") {
          addMarker(payload);
        } else {
          addEntry(payload);
        }
      } catch (error) {
        console.error("Invalid transcript message", error);
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

    socket.addEventListener("error", () => {
      socket.close();
    });
  };

  transcriptPanel.addEventListener("scroll", updateAutoScroll);
  connect();

  window.swapTranscriptSpeakers = () => {
    // [data-role] only: a marker row carries no speaker, and swapping the two
    // sides must not relabel it as one of them.
    Array.from(transcriptPanel.querySelectorAll(".transcript-speaker[data-role]")).forEach((speaker) => {
      const current = speaker.dataset.role === "prospect" ? "prospect" : "self";
      const next = current === "self" ? "prospect" : "self";
      speaker.dataset.role = next;
      speaker.classList.toggle("self", next === "self");
      speaker.classList.toggle("prospect", next === "prospect");
      speaker.textContent = next === "prospect"
        ? window.t("talktime.prospect_label")
        : window.t("talktime.you_label");
    });
  };

  window.resetTranscriptPanel = () => {
    transcriptPanel.innerHTML = "";
    _partialRows.clear();
    shouldAutoScroll = true;
    transcriptPanel.classList.remove("paused");
  };
}
})();
