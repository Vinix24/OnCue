/**
 * Scripted replay timeline for the OnCue launch demo — Dutch (NL) variant.
 *
 * Mirrors demo-timeline.js line-for-line (same schedule shape, same
 * timestamps) but with a fully Dutch conversation, so the objection card
 * (and everything around it) renders in Dutch instead of English.
 *
 * Loaded LAST, after every real dashboard module has already connected its
 * fake WebSocket (see demo-shim.js). This file only pushes messages into
 * those sockets on a fixed schedule — it contains no rendering logic of its
 * own, so every panel on screen is the real dashboard code doing its real
 * job against scripted input.
 *
 * The conversation: a B2B discovery/demo call. Pain-point categories are
 * taken from config/pain_points.yaml (handmatig_werk, rapportage,
 * capaciteit). The objection category ("prijs") is taken verbatim from
 * config/objections.yaml, with the response drawn verbatim from the
 * "reframe" phase of config/objection_responses.yaml. Because the objection
 * carries a response_suggestion, the real objections.js routes it to the top
 * hero spotlight (#monologue-warning) as the "say this now" cue — category +
 * counter-response — where it holds the hero briefly before the hero returns
 * to its calm default line. The buying-signal
 * category ("koopsignaal") is taken from config/opportunities.yaml, and its
 * trigger phrase is built from utterances verbatim from that same file.
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
  // The call — ~57s, 11 alternating lines. Same beats and timestamps as
  // demo-timeline.js, translated to Dutch.
  // ------------------------------------------------------------------
  say(
    "self",
    0,
    3600,
    "Bedankt dat je de tijd neemt. Voordat we ergens anders over beginnen: hoe gaat rapportage nu bij jullie?",
  );
  say(
    "prospect",
    4200,
    9000,
    "Eerlijk gezegd doen we nog heel veel handmatig. We halen elke week de cijfers uit drie verschillende spreadsheets.",
  );
  say("self", 9600, 12200, "Hoelang ben je bezig om het rapport zelf samen te stellen?");
  say(
    "prospect",
    12800,
    17600,
    "Een paar dagen elke keer, en tegen de tijd dat het klaar is, zijn de cijfers alweer veranderd.",
  );
  say(
    "self",
    18200,
    24200,
    "We hebben een logistiek bedrijf in een vergelijkbare situatie geholpen om die cyclus van twee dagen terug te brengen naar dezelfde dag, met dezelfde drie databronnen, zonder extra personeel.",
  );
  say(
    "prospect",
    24800,
    30200,
    "Dat zou helpen. We zitten ook krap in personeel, we groeien maar kunnen niet snel genoeg aannemen, dus mensen doen twee of drie rollen tegelijk.",
  );
  say(
    "self",
    30800,
    34400,
    "Logisch. Nog één ding voor we het over de cijfers hebben: heeft jullie team hier al eerder naar gekeken?",
  );
  say(
    "prospect",
    35000,
    39400,
    "Zeker. Eerlijk gezegd wordt de prijs van dit soort tools bij ons altijd een stretch dit jaar.",
  );
  say(
    "self",
    40000,
    44800,
    "Begrijpelijke vraag. Zie je dit als pure kosten, of als tijd die je echt terugkrijgt voor het team?",
  );
  say(
    "prospect",
    45400,
    50600,
    "Als je het zo bekijkt: als het ons echt zoveel tijd bespaart, wil ik graag verder en een vervolgstap zien.",
  );
  say("self", 51200, 54200, "Laten we een korte pilot inplannen rond jullie rapportageproces, deze week nog.");

  // ------------------------------------------------------------------
  // Detection cards — categories from the real Dutch config files.
  // ------------------------------------------------------------------

  // Pain point 1: handmatig_werk (config/pain_points.yaml) — no case matched.
  at(9400, () =>
    emit("pain-points", {
      type: "pain_point",
      category: "handmatig_werk",
      label: "Handmatig werk",
      confidence: 0.86,
      trigger_phrase: "we halen elke week de cijfers uit drie verschillende spreadsheets",
      timestamp_ms: 9400,
      live: true,
      provisional: false,
      case_matched: false,
      response_suggestion: "",
    }),
  );

  // Pain point 2: rapportage (config/pain_points.yaml) — case matched.
  at(18000, () =>
    emit("pain-points", {
      type: "pain_point",
      category: "rapportage",
      label: "Rapportage",
      confidence: 0.91,
      trigger_phrase: "tegen de tijd dat het klaar is, zijn de cijfers alweer veranderd",
      timestamp_ms: 18000,
      live: true,
      provisional: false,
      case_matched: true,
      case_id: "case-024",
      case_title: "Van Doorn Logistiek — rapportagecyclus van twee dagen naar dezelfde dag",
      response_suggestion:
        "Van Doorn Logistiek bracht hun rapportagecyclus terug van twee dagen naar dezelfde dag — zelfde drie databronnen, geen extra personeel.",
    }),
  );

  // Coaching suggestion — next-best-question, right after the case-matched pain point lands.
  at(18600, () =>
    emit("suggestions", {
      type: "suggestion",
      questions: [
        "Wat zou het betekenen voor je team als die rapportagecyclus terugging naar dezelfde dag?",
        "Wie controleert de cijfers nog een keer zodra het rapport eindelijk klaar is?",
        "Hoe vaak veranderen die cijfers nog voordat het rapport bij je manager ligt?",
      ],
      timestamp_ms: 18600,
    }),
  );

  // Coaching alert — monologue warning, during the rep's longer case-story line.
  at(23200, () =>
    emit("coaching", {
      type: "coaching_alert",
      alert_type: "monologue_warning",
      message: "Je bent al even aan het woord — geef het terug met een vraag.",
      severity: "amber",
      timestamp_ms: 23200,
    }),
  );

  // Pain point 3: capaciteit (config/pain_points.yaml) — no case matched.
  at(30600, () =>
    emit("pain-points", {
      type: "pain_point",
      category: "capaciteit",
      label: "Capaciteit",
      confidence: 0.83,
      trigger_phrase: "we groeien maar kunnen niet snel genoeg aannemen",
      timestamp_ms: 30600,
      live: true,
      provisional: false,
      case_matched: false,
      response_suggestion: "",
    }),
  );

  // Objection: prijs (config/objections.yaml), response from config/objection_responses.yaml (reframe phase).
  // The response_suggestion also lights the top hero spotlight via objections.js
  // (category + counter-response as the "say this now" cue), until the next
  // coaching signal or the spotlight timeout hands the hero back.
  at(39800, () =>
    emit("objections", {
      type: "objection",
      category: "prijs",
      confidence: 0.87,
      trigger_phrase: "de prijs van dit soort tools wordt bij ons altijd een stretch dit jaar",
      response_suggestion: "Zie je dit als kosten of als investering in capaciteit?",
      timestamp_ms: 39800,
    }),
  );

  // Buying signal ("koopsignaal" route from config/opportunities.yaml).
  at(51000, () =>
    emit("buying-signals", {
      type: "buying_signal",
      category: "koopsignaal",
      confidence: 0.84,
      trigger_phrase:
        "ik wil hier het liefst zo snel mogelijk mee starten, stuur maar een voorstel dan kijken we hoe we dit oppakken",
      response_suggestion:
        "Mooi momentum — vraag wat de timing drijft en leg de volgende stap vast voor het gesprek eindigt.",
      timestamp_ms: 51000,
    }),
  );

  // Conversation summary — fires after the call, then the panel is opened on screen.
  at(55200, () =>
    emit("summary", {
      type: "summary",
      text:
        "Prospect zit vast in handmatige rapportage en krappe capaciteit, duwde één keer terug op prijs, maar reageerde goed op de tijd-reframe en is klaar voor een pilot.",
      key_moments: [
        { timestamp_ms: 9400, description: "Handmatig werk pijnpunt gedetecteerd" },
        { timestamp_ms: 18000, description: "Rapportage pijnpunt — case matched (Van Doorn Logistiek)" },
        { timestamp_ms: 39800, description: "Prijsbezwaar geuit" },
        { timestamp_ms: 51000, description: "Koopsignaal — klaar om verder te gaan" },
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
