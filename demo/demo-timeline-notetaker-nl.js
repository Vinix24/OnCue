/**
 * Scripted replay timeline for the OnCue notetaker demo (Dutch).
 *
 * Selected with ?mode=notetaker&lang=nl (see the loader at the bottom of index.html).
 * Like demo-timeline.js it only pushes messages into the fake sockets of
 * demo-shim.js, so the transcript and the report view on screen are the real
 * dashboard code doing its real job against scripted input.
 *
 * What the notetaker preset shows (config/presets.yaml): the live transcript of
 * both sides, no detection, no talk-time coaching. The two sides carry the
 * neutral names the preset gives them by language ("Ik" and "Gesprekspartner"),
 * set in the demo catalog by demo-shim.js. The call ends with Stop & report,
 * the post-call report view, and the Markdown download, whose last section is
 * the transcript. The conversation is the fictional discovery call of
 * demo-timeline-nl.js: no real names, companies or customer data.
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
        speaker_labels: { self: "Ik", prospect: "Gesprekspartner" },
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
