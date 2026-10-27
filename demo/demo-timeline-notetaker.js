/**
 * Scripted replay timeline for the OnCue notetaker demo (English).
 *
 * Selected with ?mode=notetaker (see the loader at the bottom of index.html).
 * Like demo-timeline.js it only pushes messages into the fake sockets of
 * demo-shim.js, so the transcript and the report view on screen are the real
 * dashboard code doing its real job against scripted input.
 *
 * What the notetaker preset shows (config/presets.yaml): the live transcript of
 * both sides, no detection, no talk-time coaching. The two sides carry the
 * neutral names the preset gives them by language ("Me" and "Other"),
 * set in the demo catalog by demo-shim.js. The call ends with Stop & report,
 * the post-call report view, and the Markdown download, whose last section is
 * the transcript. The conversation is the fictional discovery call of
 * demo-timeline.js: no real names, companies or customer data.
 */
(() => {
  "use strict";

  const schedule = [];
  const at = (ms, fn) => schedule.push({ ms, fn });
  const emit = (channel, payload) => window.__demoEmit(channel, payload);

  const segments = [];
  let selfMs = 0;
  let prospectMs = 0;

  /** Schedule one transcript line: a partial (typing) update, then the final line. */
  const say = (speaker, startMs, endMs, text) => {
    const words = text.split(" ");
    const partialWordCount = Math.max(1, Math.round(words.length * 0.55));
    const partialText = words.slice(0, partialWordCount).join(" ");
    const partialAt = startMs + Math.round((endMs - startMs) * 0.35);

    at(startMs, () => {
      emit("transcript", { type: "partial_transcript", text: partialText, speaker, start_ms: startMs });
    });

    if (partialWordCount < words.length && partialAt < endMs) {
      at(partialAt, () => {
        emit("transcript", { type: "partial_transcript", text: words.join(" "), speaker, start_ms: startMs });
      });
    }

    at(endMs, () => {
      emit("transcript", {
        type: "transcript",
        text,
        speaker,
        start_ms: startMs,
        end_ms: endMs,
        is_final: true,
      });
    });

    segments.push({ speaker, text, start_ms: startMs, end_ms: endMs });
    if (speaker === "self") {
      selfMs += endMs - startMs;
    } else {
      prospectMs += endMs - startMs;
    }
  };

  // ------------------------------------------------------------------
  // The call: ~55s, 11 alternating lines.
  // ------------------------------------------------------------------
  say("self", 0, 3600, "Thanks for jumping on. Before anything else, how does reporting work for your team today?");
  say(
    "prospect",
    4200,
    9000,
    "Honestly, a lot of it's still manual. We're pulling numbers from three different spreadsheets every week.",
  );
  say("self", 9600, 12200, "How long does putting the actual report together take?");
  say(
    "prospect",
    12800,
    17600,
    "A couple of days each time, and by the time it's out the numbers have already changed.",
  );
  say(
    "self",
    18200,
    24200,
    "We helped a logistics team in a very similar spot cut that cycle from two days to same-day, same three data sources, no extra headcount.",
  );
  say(
    "prospect",
    24800,
    30200,
    "That would help. We're also stretched thin, we're growing but can't hire fast enough, so people cover two or three roles at once.",
  );
  say(
    "self",
    30800,
    34400,
    "Makes sense. Quick one before we get into numbers, has your team looked at anything like this before?",
  );
  say(
    "prospect",
    35000,
    39400,
    "We have. Honestly the pricing on tools like this always ends up being a stretch for us this year.",
  );
  say(
    "self",
    40000,
    44800,
    "Fair question. Are you thinking about this as a straight cost, or as time you're actually getting back for the team?",
  );
  say(
    "prospect",
    45400,
    50600,
    "When you put it that way, if it really saves us that much time, I'd like to move forward and see a next step.",
  );
  say("self", 51200, 54200, "Let's set up a short pilot around your reporting workflow this week.");

  const CALL_END_MS = 54200;

  // ------------------------------------------------------------------
  // The call starts: the dashboard applies the notetaker preset itself (served
  // by the shim's /api/presets) and the sidebar shows Stop & report, the way a
  // running call looks.
  // ------------------------------------------------------------------
  const startCall = () => {
    if (window.showCallView) {
      window.showCallView();
    }
  };

  // ------------------------------------------------------------------
  // Stop & report: the real end-call button, then the report the backend
  // writes. The report carries the transcript, so the Markdown download the
  // dashboard builds from it ends with it.
  // ------------------------------------------------------------------
  const total = Math.max(1, selfMs + prospectMs);

  at(CALL_END_MS + 1800, () => {
    const button = document.getElementById("end-call");
    if (button) {
      button.click();
    }
  });

  at(CALL_END_MS + 2600, () => {
    emit("coaching", {
      type: "report_ready",
      report: {
        session_id: "demo-notetaker",
        call_duration_ms: CALL_END_MS,
        total_self_pct: selfMs / total,
        total_prospect_pct: prospectMs / total,
        monologue_count: 0,
        conversation_summary: "",
        full_transcript: segments,
        speaker_labels: { self: "Me", prospect: "Other" },
      },
    });
    emit("config", { type: "call_ended" });
  });

  at(CALL_END_MS + 9500, () => {
    const download = document.getElementById("report-download-markdown");
    if (download) {
      download.click();
    }
  });

  // ------------------------------------------------------------------
  // Runner + Replay control
  // ------------------------------------------------------------------
  const runSchedule = () => {
    schedule
      .slice()
      .sort((a, b) => a.ms - b.ms)
      .forEach(({ ms, fn }) => {
        setTimeout(fn, ms);
      });
  };

  const start = async () => {
    if (window.SalesCopilotI18n && window.SalesCopilotI18n.ready) {
      await window.SalesCopilotI18n.ready;
    }
    if (window.startCallSockets) {
      window.startCallSockets();
    }
    startCall();
    // Small buffer so the connection pill visibly flips to "Connected"
    // (fake sockets open ~40ms after construction) before the call starts.
    setTimeout(runSchedule, 300);
  };

  if (document.readyState === "complete") {
    start();
  } else {
    window.addEventListener("load", start);
  }

  const replay = () => window.location.reload();

  const replayBtn = document.getElementById("demo-replay-btn");
  if (replayBtn) {
    replayBtn.addEventListener("click", replay);
  }
  window.addEventListener("keydown", (event) => {
    if ((event.key === "r" || event.key === "R") && !event.metaKey && !event.ctrlKey && !event.altKey) {
      replay();
    }
  });
})();
