/**
 * Scripted replay timeline for the OnCue launch demo.
 *
 * Loaded LAST, after every real dashboard module has already connected its
 * fake WebSocket (see demo-shim.js). This file only pushes messages into
 * those sockets on a fixed schedule — it contains no rendering logic of its
 * own, so every panel on screen is the real dashboard code doing its real
 * job against scripted input.
 *
 * The conversation: a B2B discovery/demo call. Pain-point categories are
 * taken from config/pain_points_en.yaml (manual_work, reporting, capacity).
 * The objection category is taken from config/objections_en.yaml (price),
 * with a response drawn from config/objection_responses_en.yaml. Because the
 * objection carries a response_suggestion, the real objections.js routes it
 * to the top hero spotlight (#monologue-warning) as the "say this now" cue —
 * category + counter-response — where it holds the hero briefly before the
 * hero returns to its calm default line. The buying
 * signal represents the "koopsignaal" route from config/opportunities.yaml
 * (English label "Buying signal" — there is no English opportunities config
 * yet, see the PR description for that gap).
 */
(() => {
  "use strict";

  const schedule = [];
  const at = (ms, fn) => schedule.push({ ms, fn });
  const emit = (channel, payload) => window.__demoEmit(channel, payload);

  let selfMs = 0;
  let prospectMs = 0;

  const talkTimePayload = (callDurationMs) => {
    const total = Math.max(1, selfMs + prospectMs);
    return {
      type: "talk_time",
      rolling_self_pct: selfMs / total,
      rolling_prospect_pct: prospectMs / total,
      cumulative_self_pct: selfMs / total,
      cumulative_prospect_pct: prospectMs / total,
      current_monologue_ms: 0,
      monologue_speaker: null,
      call_duration_ms: callDurationMs,
      phase: "discovery",
      status: "green",
    };
  };

  /** Schedule one transcript line: a partial (typing) update, then the final line + a talk-time tick. */
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
      if (speaker === "self") {
        selfMs += endMs - startMs;
      } else {
        prospectMs += endMs - startMs;
      }
      emit("talk-time", talkTimePayload(endMs));
    });
  };

  // ------------------------------------------------------------------
  // The call — ~57s, 11 alternating lines.
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

  // ------------------------------------------------------------------
  // Detection cards — categories from the real English config files.
  // ------------------------------------------------------------------

  // Pain point 1: manual_work (config/pain_points_en.yaml) — no case matched.
  at(9400, () =>
    emit("pain-points", {
      type: "pain_point",
      category: "manual_work",
      label: "Manual work",
      confidence: 0.86,
      trigger_phrase: "we're pulling numbers from three different spreadsheets every week",
      timestamp_ms: 9400,
      live: true,
      provisional: false,
      case_matched: false,
      response_suggestion: "",
    }),
  );

  // Pain point 2: reporting (config/pain_points_en.yaml) — case matched.
  at(18000, () =>
    emit("pain-points", {
      type: "pain_point",
      category: "reporting",
      label: "Reporting",
      confidence: 0.91,
      trigger_phrase: "by the time it's out the numbers have already changed",
      timestamp_ms: 18000,
      live: true,
      provisional: false,
      case_matched: true,
      case_id: "case-024",
      case_title: "Vantage Logistics — same-day reporting instead of two days",
      response_suggestion:
        "Vantage Logistics cut their reporting cycle from two days to same-day — same three data sources, zero added headcount.",
    }),
  );

  // Coaching suggestion — next-best-question, right after the case-matched pain point lands.
  at(18600, () =>
    emit("suggestions", {
      type: "suggestion",
      questions: [
        "What would getting that reporting cycle down to same-day mean for your team?",
        "Who ends up double-checking the numbers once the report is finally out?",
        "How often do those numbers shift before the report even reaches your manager?",
      ],
      timestamp_ms: 18600,
    }),
  );

  // Coaching alert — monologue warning, during the rep's longer case-story line.
  at(23200, () =>
    emit("coaching", {
      type: "coaching_alert",
      alert_type: "monologue_warning",
      message: "You've been talking for a while — hand it back with a question.",
      severity: "amber",
      timestamp_ms: 23200,
    }),
  );

  // Pain point 3: capacity (config/pain_points_en.yaml) — no case matched.
  at(30600, () =>
    emit("pain-points", {
      type: "pain_point",
      category: "capacity",
      label: "Capacity",
      confidence: 0.83,
      trigger_phrase: "we're growing but can't hire fast enough",
      timestamp_ms: 30600,
      live: true,
      provisional: false,
      case_matched: false,
      response_suggestion: "",
    }),
  );

  // Objection: price (config/objections_en.yaml), response from config/objection_responses_en.yaml (reframe phase).
  // The response_suggestion also lights the top hero spotlight via objections.js
  // (category + counter-response as the "say this now" cue), until the next
  // coaching signal or the spotlight timeout hands the hero back.
  at(39800, () =>
    emit("objections", {
      type: "objection",
      category: "price",
      confidence: 0.87,
      trigger_phrase: "the pricing on tools like this always ends up being a stretch for us this year",
      response_suggestion: "Are you seeing this as a cost, or as an investment in getting that time back for the team?",
      timestamp_ms: 39800,
    }),
  );

  // Buying signal ("koopsignaal" route from config/opportunities.yaml).
  at(51000, () =>
    emit("buying-signals", {
      type: "buying_signal",
      category: "buying_signal",
      confidence: 0.84,
      trigger_phrase: "if it really saves us that much time, I'd like to move forward and see a next step",
      response_suggestion:
        "Great momentum — ask what's driving the timing and lock in the next step before the call ends.",
      timestamp_ms: 51000,
    }),
  );

  // Conversation summary — fires after the call, then the panel is opened on screen.
  at(55200, () =>
    emit("summary", {
      type: "summary",
      text:
        "Prospect is bogged down by manual reporting and tight capacity, pushed back once on price, but responded well to the time-reframe and is ready for a pilot.",
      key_moments: [
        { timestamp_ms: 9400, description: "Manual work pain point detected" },
        { timestamp_ms: 18000, description: "Reporting pain point — case matched (Vantage Logistics)" },
        { timestamp_ms: 39800, description: "Price objection raised" },
        { timestamp_ms: 51000, description: "Buying signal — ready to move forward" },
      ],
      timestamp_ms: 55200,
    }),
  );

  at(56200, () => {
    const toggle = document.getElementById("summary-toggle");
    if (toggle && toggle.getAttribute("aria-expanded") !== "true") {
      toggle.click();
    }
  });

  // ------------------------------------------------------------------
  // Runner + Replay control
  // ------------------------------------------------------------------
  const timers = [];

  const runSchedule = () => {
    schedule
      .slice()
      .sort((a, b) => a.ms - b.ms)
      .forEach(({ ms, fn }) => {
        timers.push(setTimeout(fn, ms));
      });
  };

  const start = async () => {
    if (window.SalesCopilotI18n && window.SalesCopilotI18n.ready) {
      await window.SalesCopilotI18n.ready;
    }
    // talk-time / coaching / phase / config only connect once app.js's
    // startCallSockets() runs — normally gated behind clicking "Start Call".
    // The demo has no click to make, so open them the same way a real call
    // start would, before anything gets emitted on those channels.
    if (window.startCallSockets) {
      window.startCallSockets();
    }
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
