/**
 * Replay shim for the OnCue launch demo.
 *
 * Loaded FIRST, before any real dashboard module (js/i18n.js, js/app.js, ...).
 * It replaces window.fetch and window.WebSocket so every dashboard module
 * runs completely unmodified against a fake backend:
 *
 *   - window.fetch is stubbed for the handful of endpoints the dashboard
 *     needs a real answer from to render correctly under file:// (where
 *     fetch() of local files/CORS is blocked): /api/config, the i18n
 *     catalogs (English by default, Dutch when ?lang=nl is set), and the
 *     Pro license endpoints (so the demo shows the full Pro experience
 *     instead of a Free upgrade banner). Every other endpoint (presets,
 *     llm-models, upload, start-call, ...) is left to fail closed exactly
 *     like it would with no backend running — every caller in the real
 *     dashboard code already handles that gracefully.
 *   - window.WebSocket is replaced with a fake socket that dashboard modules
 *     construct exactly as they would against a live hub. demo-timeline.js
 *     (English) or demo-timeline-nl.js (?lang=nl), loaded last after every
 *     module has connected, pushes scripted messages into these sockets via
 *     window.__demoEmit(channel, payload).
 */
(() => {
  "use strict";

  // Each fresh load (and every Replay via location.reload()) starts from a
  // clean slate, regardless of what a previous run left in this file://
  // origin's localStorage (theme choice, setup form state, consent state).
  localStorage.setItem("dashboard-theme", "light");
  localStorage.removeItem("sales-copilot-setup");
  localStorage.removeItem("sales-copilot-consent");

  // Language selection — ?lang=nl switches the demo to Dutch (chrome catalog
  // below + demo-timeline-nl.js, loaded conditionally at the bottom of
  // index.html). Any other value (or none) keeps the English default.
  const ACTIVE_LANG = new URLSearchParams(window.location.search).get("lang") === "nl" ? "nl" : "en";

  // ---------------------------------------------------------------------
  // Inlined English i18n catalog — verbatim copy of dashboard/i18n/en.json.
  // dashboard/js/i18n.js fetches dashboard/i18n/<lang>.json at runtime; under
  // file:// that fetch is CORS-blocked, so without this the whole dashboard
  // would render raw i18n keys ("header.status_disconnected") instead of
  // English copy. Keep this in sync with dashboard/i18n/en.json by hand if
  // that file changes.
  // ---------------------------------------------------------------------
  const EN_CATALOG = {
    setup: {
      privacy_notice: "Audio is recorded locally for playback. Never uploaded.",
      sample_aha_button: "Show me what it does",
      screen_mode_help: "1-screen mode disables the presentation module.",
      subtitle: "Settings for realtime coaching and slides.",
      screen_mode_title: "Screen mode",
      screen_mode_group_aria: "Screen mode",
      screen_mode_single: "1 Screen",
      screen_mode_dual: "2 Screens",
      presets_title: "Presets",
      conversation_type_label: "Conversation type",
      preset_sales: "Sales",
      preset_coach: "Coach",
      preset_recruitment: "Recruitment",
      modules_title: "Modules",
      module_talk_time: "Talk Time",
      module_transcript: "Live Transcript",
      module_pain_points: "AI detection",
      detection_off_indicator: "AI detection is off for this call.",
      module_presentation: "Slide Injector",
      module_post_call_report: "Post-call Report",
      llm_prospect_title: "LLM & Prospect",
      llm_provider_label: "LLM provider & model",
      loading_option: "Loading...",
      transcript_backend_label: "Transcript backend",
      transcript_backend_info:
        "whisper.cpp and MLX Whisper run locally — audio never leaves the machine. Groq Whisper is CLOUD: every audio fragment goes to Groq's API. Do not use for sensitive or confidential conversations.",
      transcript_whispercpp: "whisper.cpp (default)",
      transcript_mlx: "MLX Whisper (experimental)",
      transcript_groq: "Groq Whisper (cloud, fast)",
      call_medium_label: "Call medium",
      own_transcript_label: "Own transcript live",
      own_transcript_info:
        "By default you only see the prospect during the call (faster and more stable). Enable this to see your own text live. whisper.cpp is the default backend and supports this most reliably; MLX Whisper is experimental.",
      show_toggle: "Show",
      client_label: "Client",
      client_none_option: "No client",
      client_info: "Picks a client folder under KLANTEN_ROOT. Company, industry, call documents and the privacy gate then come from klant.yaml.",
      documents_title: "Documents",
      dropzone_text: "Drop quote docs or cases here",
      dropzone_filetypes: "PDF, DOCX, or TXT",
      status_ready: "Ready",
      start_call_button: "Start Call",
      end_call_button: "Stop & report",
      call_medium_video_option: "Teams / Meet / videocall",
      call_medium_phone_option: "Phone",
      subtitle_no_slides: "Pre-call setup for realtime coaching.",
      sample_aha_playing: "Playing sample...",
      sample_aha_loading: "Loading sample...",
      sample_aha_running: "Demo running...",
      sample_aha_start_failed: "Sample start failed ({status}): {detail}",
      sample_aha_failed_generic: "Playing sample failed. Check that the backend is running.",
      preset_default_label: "Default",
      preset_fallback_label: "Preset {index}",
      remove_file_button: "Remove",
      upload_failed: "Upload failed: {filename}",
      status_starting: "Starting...",
      status_connecting: "Connecting",
      status_in_call: "In Call",
      status_connection_failed: "Connection failed",
      start_call_failed_generic:
        "Start Call failed: check that the backend/hub is running on the correct WS host/port.",
      llm_model_placeholder: "Select a model",
    },
    common: {
      close_dialog: "Close notification",
      close_window: "Close window",
      live_badge: "live",
      category_unknown: "Unknown",
      no_suggestion_available: "No suggestion available",
    },
    feedback: {
      useful_label: "Used / useful",
      not_useful_label: "Not useful",
      useful_aria: "Mark this suggestion as used / useful",
      not_useful_aria: "Mark this suggestion as not useful",
    },
    license: {
      email_placeholder: "you@email.com",
      banner_text: "You're using the free version. Request a license key for extra features.",
      get_free_key: "Get free key",
      modal_title: "Free bonus + license key",
      modal_desc: "Enter your email address and get a free license key instantly.",
      email_label: "Email address",
      submit_button: "Send me the key",
      success_message: "License key sent. Check your inbox.",
      error_message: "Something went wrong. Please try again.",
    },
    degrade: {
      banner_intro: "Your Pro license is no longer active. The app keeps working for free:",
      banner_active_features: "live transcript, summary and post-call diagnosis",
      banner_active_suffix: "remain active.",
      banner_disabled_features: "Live coaching, slide injection, autostart and central audit are disabled.",
      upgrade_cta: "Upgrade to Sales Pro",
    },
    pro_upgrade: {
      modal_title: "Sales Pro feature",
      desc_intro: "Phone capture (FaceTime / iPhone relay via AudioTee) is part of",
      desc_outro: "Upgrade to get live coaching during real phone calls.",
      cta: "View Sales Pro",
      later: "Maybe later",
      badge_label: "🔒 Pro",
    },
    consent: {
      title: "Consent",
      checkbox_label: "I have asked for and received consent for this session.",
      tier_badge_audit: "audit",
      tier_badge_off: "off",
      tier_badge_soft: "soft",
      tier_badge_strict: "strict",
      status_not_recorded: "not recorded",
      status_recorded: "consent recorded",
      hint_strict_given: "Consent has been recorded; you can use Start Call.",
      hint_strict_missing: "Check that you have received consent to start the session.",
      hint_soft_given: "Consent has been recorded.",
      hint_soft_missing: "Note: consent has not been recorded yet. The session will still start.",
      not_recorded_warning: "Consent has not been recorded yet.",
      call_blocked_alert: "Start Call blocked: consent is required in strict mode.",
    },
    header: {
      no_prospect_set: "No prospect set yet",
      phase_group_label: "Call phase",
      phase_discovery: "Discovery",
      phase_pitch: "Pitch",
      phase_closing: "Closing",
      phase_auto_indicator: "auto",
      swap_speakers: "Swap Speakers",
      status_disconnected: "Disconnected",
      status_connected: "Connected",
      status_reconnecting: "Reconnecting",
      theme_toggle_title: "Dark mode",
      theme_toggle_to_dark: "Dark mode",
      theme_toggle_to_light: "Light mode",
      system_status_warming_default: "Warming up AI...",
      audio_warning_self_label: "my microphone",
      audio_warning_prospect_label: "the other party",
      audio_warning_message: "No audio on {label} — check your audio settings",
      stop_server_title: "Stop OnCue",
      stop_server_confirm: "Stop OnCue? The server will shut down.",
      stop_server_done_title: "OnCue stopped",
      stop_server_done_message: "Server has shut down. You can close this browser tab.",
    },
    coaching: {
      suggestion_empty_state: "Waiting for conversation context...",
      signal_kicker: "Coaching signal",
      monologue_default:
        "I'm listening for buying signals and objections. As soon as something comes up, you'll see the best next move here.",
      time_to_listen_default: "Time to listen.",
    },
    transcript: {
      title: "Transcript",
      live_listening: "Live listening",
      markers_hint: "pain points & objections are marked",
    },
    objections: {
      title: "Objections",
      empty_state: "No objections detected yet.",
      fallback_label: "Objection",
    },
    painpoints: {
      title: "Pain points",
      empty_state: "No detections yet.",
      case_match_label: "→ Case {caseId}",
      no_case_match: "No case match",
    },
    detector: {
      status_waiting: "Detection not started yet",
      status_listening: "Listening, nothing in yet",
      status_active: "Listening · {received} heard · {detections} detected",
      status_no_input: "Listening, but nothing is coming in",
      status_stale: "No signal from detection",
      status_stopped: "Detection stopped",
      toggle_title: "Show what detection is doing",
      reason_waiting: "Detection has not reported in yet. Start the call, or check that AI detection is switched on.",
      reason_stale: "Detection gave a last sign of life and then went quiet. The module has most likely stopped.",
      reason_no_input: "Detection is running, but no speech has come through yet. Check transcription and audio input.",
      reason_nothing_received: "No speech has come through yet.",
      reason_healthy: "{detections} detections sent to the dashboard.",
      reason_not_prospect: "{count} utterances skipped: that was your own speech, detection only looks at the prospect.",
      reason_below_min: "Not enough context yet: {min} sentences are needed for a classification.",
      reason_debounced: "Skipped {count} times because the previous classification was too recent.",
      reason_low_confidence: "{count} matches fell below the confidence threshold.",
      reason_dropped_none: "Classified as 'no pain point' {count} times.",
      reason_nothing_matched: "Speech is coming in, nothing has surfaced yet.",
      count_received: "Received",
      count_skipped_not_prospect: "Not prospect",
      count_buffered_below_min: "Too little context",
      count_debounced: "Too soon after previous",
      count_classified: "Classified",
      count_dropped_none: "No pain point",
      count_dropped_low_confidence: "Below threshold",
      count_dispatched: "Surfaced",
      confidence_high: "Strong match",
      confidence_uncertain: "Weak match, not confirmed",
    },
    opportunities: {
      title: "Opportunities",
      empty_state: "No opportunities detected yet.",
    },
    talktime: {
      title: "Talking time",
      you_label: "You",
      prospect_label: "Prospect",
      bar_aria: "Talk time ratio",
      total_label: "Total",
    },
    sentiment: {
      title: "Customer sentiment",
      cool: "cool",
      neutral: "neutral",
      warm: "warm",
      now_label: "Now:",
    },
    suggestions: {
      title: "Signals",
      copied_label: "Copied",
    },
    scripttracking: {
      title: "Call plan",
      coverage_badge: "coverage",
      empty_state: "No script coverage measured yet.",
      status_missing: "Not yet",
      status_partial: "Partial",
      status_tentative: "Possibly covered",
      status_confirmed: "Confirmed",
      health_paused: "Live tracking paused — showing the last known state.",
      nudge_default: "Come back to the most important open points.",
      pro_gate_message: "Script coverage is a Pro feature.",
      upgrade_button: "Upgrade",
    },
    summary: {
      title: "Summary",
      empty_state: "No summary available yet.",
      empty_moments: "No key moments yet.",
      key_moment_fallback_label: "Key moment",
    },
    report: {
      title: "Post-call Report",
      call_duration_label: "Call duration",
      talktime_label: "Talk-time",
      monologues_label: "Monologues",
      scorecard_title: "Scorecard (detected)",
      pain_points_title: "Pain points detected",
      objections_title: "Objections raised",
      key_moments_title: "Key moments",
      final_summary_title: "Final conversation summary",
      download_json: "Download Report (JSON)",
      download_markdown: "Download Report (Markdown)",
      new_call_button: "New Call",
      key_moment_fallback_label: "Moment",
      no_scorecard: "No scorecard available.",
      no_pain_points: "No pain points detected.",
      no_objections: "No objections detected.",
      no_key_moments: "No key moments available.",
      scorecard_script_coverage: "You covered {covered} of {total} script points (detected)",
      scorecard_missing_points: "Missed: {points}",
      scorecard_no_missing_points: "No required points missed.",
      scorecard_objections_responded: "You received {count} objections, responded to {responded} (detected)",
      scorecard_opportunities_detected: "Opportunities detected: {count}",
      talktime_self_prospect_label: "Talk-time (self/prospect)",
      monologue_warnings_label: "Monologue warnings",
      markdown_none: "None",
    },
  };

  // ---------------------------------------------------------------------
  // Inlined Dutch i18n catalog — verbatim copy of dashboard/i18n/nl.json.
  // Same rationale as EN_CATALOG above: file:// blocks the real fetch, so
  // this keeps ?lang=nl showing real Dutch chrome copy instead of raw i18n
  // keys. Keep this in sync with dashboard/i18n/nl.json by hand if that file
  // changes.
  // ---------------------------------------------------------------------
  const NL_CATALOG = {
    setup: {
      privacy_notice: "Audio wordt lokaal opgenomen voor terugluisteren. Wordt niet ge-upload.",
      sample_aha_button: "Toon me wat het doet",
      screen_mode_help: "1-screen schakelt de presentatie-module uit.",
      subtitle: "Instellingen voor realtime coaching en slides.",
      screen_mode_title: "Screen mode",
      screen_mode_group_aria: "Schermmodus",
      screen_mode_single: "1 Screen",
      screen_mode_dual: "2 Screens",
      presets_title: "Presets",
      conversation_type_label: "Gesprekstype",
      preset_sales: "Sales",
      preset_coach: "Coach",
      preset_recruitment: "Recruitment",
      modules_title: "Modules",
      module_talk_time: "Talk Time",
      module_transcript: "Live Transcript",
      module_pain_points: "AI detectie",
      detection_off_indicator: "AI-detectie staat uit voor dit gesprek.",
      module_presentation: "Slide Injector",
      module_post_call_report: "Post-call Report",
      llm_prospect_title: "LLM & Prospect",
      llm_provider_label: "LLM provider & model",
      loading_option: "Laden...",
      transcript_backend_label: "Transcript backend",
      transcript_backend_info:
        "whisper.cpp en MLX Whisper draaien lokaal — audio verlaat de machine nooit. Groq Whisper is CLOUD: elk audiofragment gaat naar Groq's API. Niet gebruiken bij gevoelige of vertrouwelijke gesprekken.",
      transcript_whispercpp: "whisper.cpp (standaard)",
      transcript_mlx: "MLX Whisper (experimenteel)",
      transcript_groq: "Groq Whisper (cloud, snel)",
      call_medium_label: "Gespreksmedium",
      own_transcript_label: "Eigen transcript live",
      own_transcript_info:
        "Standaard zie je tijdens de call alleen de prospect (sneller en stabieler). Schakel dit in om je eigen tekst live te zien. whisper.cpp is de standaard-backend en ondersteunt dit het meest stabiel; MLX Whisper is experimenteel.",
      show_toggle: "Tonen",
      client_label: "Klant",
      client_none_option: "Geen klant",
      client_info: "Kiest een klantmap onder KLANTEN_ROOT. Bedrijf, industry, gespreksdocumenten en de privacy-poort komen dan uit klant.yaml.",
      documents_title: "Documenten",
      dropzone_text: "Drop offertedocs of cases hier",
      dropzone_filetypes: "PDF, DOCX, of TXT",
      status_ready: "Ready",
      start_call_button: "Start Call",
      end_call_button: "Stop & rapport",
      call_medium_video_option: "Teams / Meet / videocall",
      call_medium_phone_option: "Telefoon",
      subtitle_no_slides: "Pre-call setup voor realtime coaching.",
      sample_aha_playing: "Sample afspelen...",
      sample_aha_loading: "Sample laden...",
      sample_aha_running: "Demo loopt...",
      sample_aha_start_failed: "Sample start mislukt ({status}): {detail}",
      sample_aha_failed_generic: "Sample afspelen mislukt. Controleer of de backend draait.",
      preset_default_label: "Default",
      preset_fallback_label: "Preset {index}",
      remove_file_button: "Verwijderen",
      upload_failed: "Upload mislukt: {filename}",
      status_starting: "Starting...",
      status_connecting: "Connecting",
      status_in_call: "In Call",
      status_connection_failed: "Connection failed",
      start_call_failed_generic:
        "Start Call mislukt: controleer of de backend/hub draait op de juiste WS-host/poort.",
      llm_model_placeholder: "Selecteer een model",
    },
    common: {
      close_dialog: "Sluit melding",
      close_window: "Sluit venster",
      live_badge: "live",
      category_unknown: "Onbekend",
      no_suggestion_available: "Geen suggestie beschikbaar",
    },
    feedback: {
      useful_label: "Gebruikt / nuttig",
      not_useful_label: "Niet nuttig",
      useful_aria: "Markeer deze suggestie als gebruikt / nuttig",
      not_useful_aria: "Markeer deze suggestie als niet nuttig",
    },
    license: {
      email_placeholder: "jouw@email.nl",
      banner_text: "Je gebruikt de gratis versie. Vraag een license-key aan voor extra features.",
      get_free_key: "Krijg gratis key",
      modal_title: "Gratis bonus + license-key",
      modal_desc: "Vul je e-mailadres in en ontvang direct een gratis license-key.",
      email_label: "E-mailadres",
      submit_button: "Stuur mij de key",
      success_message: "License-key verstuurd. Check je inbox.",
      error_message: "Er ging iets mis. Probeer het opnieuw.",
    },
    degrade: {
      banner_intro: "Je Pro-licentie is niet meer actief. De app blijft gratis werken:",
      banner_active_features: "live transcript, samenvatting en post-call diagnose",
      banner_active_suffix: "blijven actief.",
      banner_disabled_features: "Live coaching, slide-injectie, autostart en centrale audit zijn uitgeschakeld.",
      upgrade_cta: "Upgrade naar Sales Pro",
    },
    pro_upgrade: {
      modal_title: "Sales Pro feature",
      desc_intro: "Telefonie-capture (FaceTime / iPhone-relay via AudioTee) zit in",
      desc_outro: "Upgrade om tijdens echte telefoongesprekken live te coachen.",
      cta: "Bekijk Sales Pro",
      later: "Misschien later",
      badge_label: "🔒 Pro",
    },
    consent: {
      title: "Toestemming",
      checkbox_label: "Ik heb toestemming gevraagd en gekregen voor deze sessie.",
      tier_badge_audit: "audit",
      tier_badge_off: "uit",
      tier_badge_soft: "soft",
      tier_badge_strict: "strict",
      status_not_recorded: "niet vastgelegd",
      status_recorded: "toestemming vastgelegd",
      hint_strict_given: "Toestemming is vastgelegd; je kunt Start Call gebruiken.",
      hint_strict_missing: "Vink aan dat je toestemming hebt gekregen om de sessie te starten.",
      hint_soft_given: "Toestemming is vastgelegd.",
      hint_soft_missing: "Let op: toestemming is nog niet vastgelegd. De sessie start wel door.",
      not_recorded_warning: "Toestemming is nog niet vastgelegd.",
      call_blocked_alert: "Start Call geblokkeerd: toestemming is verplicht in strict-modus.",
    },
    header: {
      no_prospect_set: "Nog geen prospect ingesteld",
      phase_group_label: "Gespreksfase",
      phase_discovery: "Discovery",
      phase_pitch: "Pitch",
      phase_closing: "Closing",
      phase_auto_indicator: "auto",
      swap_speakers: "Swap Speakers",
      status_disconnected: "Disconnected",
      status_connected: "Connected",
      status_reconnecting: "Reconnecting",
      theme_toggle_title: "Dark mode",
      theme_toggle_to_dark: "Dark mode",
      theme_toggle_to_light: "Light mode",
      system_status_warming_default: "AI opwarmen...",
      audio_warning_self_label: "mijn microfoon",
      audio_warning_prospect_label: "gesprekspartner",
      audio_warning_message: "Geen audio op {label} — check je audio-instellingen",
      stop_server_title: "OnCue stoppen",
      stop_server_confirm: "OnCue stoppen? Server wordt afgesloten.",
      stop_server_done_title: "OnCue gestopt",
      stop_server_done_message: "Server is afgesloten. Browser-tab kan gesloten worden.",
    },
    coaching: {
      suggestion_empty_state: "Wacht op gesprekscontext...",
      signal_kicker: "Coaching-signaal",
      monologue_default:
        "Ik let mee op koopsignalen en bezwaren. Zodra er iets speelt, zie je hier direct de beste volgende zet.",
      time_to_listen_default: "Tijd om te luisteren.",
    },
    transcript: {
      title: "Transcript",
      live_listening: "Live meeluisteren",
      markers_hint: "pijnpunten & bezwaren worden gemarkeerd",
    },
    objections: {
      title: "Bezwaren",
      empty_state: "Nog geen bezwaren gedetecteerd.",
      fallback_label: "Bezwaar",
    },
    painpoints: {
      title: "Pijnpunten",
      empty_state: "Nog geen detections binnen.",
      case_match_label: "→ Case {caseId}",
      no_case_match: "Geen case match",
    },
    detector: {
      status_waiting: "Detectie nog niet actief",
      status_listening: "Luistert, nog niets binnen",
      status_active: "Luistert · {received} gehoord · {detections} gedetecteerd",
      status_no_input: "Luistert, maar er komt niets binnen",
      status_stale: "Geen signaal van de detectie",
      status_stopped: "Detectie gestopt",
      toggle_title: "Toon wat de detectie doet",
      reason_waiting: "De detectie heeft zich nog niet gemeld. Start het gesprek, of controleer of AI-detectie aan staat.",
      reason_stale: "De detectie gaf een laatste teken van leven en is daarna stil gevallen. Waarschijnlijk is de module gestopt.",
      reason_no_input: "De detectie draait, maar er is nog geen spraak doorgekomen. Controleer de transcriptie en de audio-invoer.",
      reason_nothing_received: "Nog geen spraak doorgekomen.",
      reason_healthy: "{detections} detecties naar het dashboard gestuurd.",
      reason_not_prospect: "{count} uitingen overgeslagen: dat was jouw spraak, de detectie kijkt alleen naar de prospect.",
      reason_below_min: "Nog te weinig context: er zijn {min} zinnen nodig voor een classificatie.",
      reason_debounced: "{count} keer overgeslagen omdat de vorige classificatie nog te kort geleden was.",
      reason_low_confidence: "{count} matches vielen onder de betrouwbaarheidsdrempel.",
      reason_dropped_none: "{count} keer geclassificeerd als 'geen pijnpunt'.",
      reason_nothing_matched: "Spraak komt binnen, er kwam nog niets bovendrijven.",
      count_received: "Binnengekomen",
      count_skipped_not_prospect: "Niet van prospect",
      count_buffered_below_min: "Te weinig context",
      count_debounced: "Te snel na vorige",
      count_classified: "Geclassificeerd",
      count_dropped_none: "Geen pijnpunt",
      count_dropped_low_confidence: "Onder drempel",
      count_dispatched: "Getoond",
      confidence_high: "Sterke match",
      confidence_uncertain: "Zwakke match, niet bevestigd",
    },
    opportunities: {
      title: "Kansen",
      empty_state: "Nog geen kansen gedetecteerd.",
    },
    talktime: {
      title: "Talking time",
      you_label: "Jij",
      prospect_label: "Prospect",
      bar_aria: "Talk time verhouding",
      total_label: "Totaal",
    },
    sentiment: {
      title: "Klantstemming",
      cool: "koel",
      neutral: "neutraal",
      warm: "warm",
      now_label: "Nu:",
    },
    suggestions: {
      title: "Signalen",
      copied_label: "Gekopieerd",
    },
    scripttracking: {
      title: "Gespreksplan",
      coverage_badge: "dekking",
      empty_state: "Nog geen scriptdekking gemeten.",
      status_missing: "Nog niet",
      status_partial: "Gedeeltelijk",
      status_tentative: "Mogelijk geraakt",
      status_confirmed: "Bevestigd",
      health_paused: "Live tracking gepauzeerd — laatst bekende stand wordt getoond.",
      nudge_default: "Kom terug op de belangrijkste openstaande punten.",
      pro_gate_message: "Script Dekking is een Pro feature.",
      upgrade_button: "Upgrade",
    },
    summary: {
      title: "Samenvatting",
      empty_state: "Nog geen samenvatting beschikbaar.",
      empty_moments: "Nog geen key moments.",
      key_moment_fallback_label: "Key moment",
    },
    report: {
      title: "Post-call Report",
      call_duration_label: "Call duration",
      talktime_label: "Talk-time",
      monologues_label: "Monologues",
      scorecard_title: "Scorecard (gedetecteerd)",
      pain_points_title: "Pain points detected",
      objections_title: "Objections raised",
      key_moments_title: "Key moments",
      final_summary_title: "Final conversation summary",
      download_json: "Download Report (JSON)",
      download_markdown: "Download Report (Markdown)",
      new_call_button: "New Call",
      key_moment_fallback_label: "Moment",
      no_scorecard: "Geen scorecard beschikbaar.",
      no_pain_points: "Geen pain points gedetecteerd.",
      no_objections: "Geen bezwaren gedetecteerd.",
      no_key_moments: "Geen key moments beschikbaar.",
      scorecard_script_coverage: "Je dekte {covered} van {total} script-punten (gedetecteerd)",
      scorecard_missing_points: "Gemist: {points}",
      scorecard_no_missing_points: "Geen verplichte punten gemist.",
      scorecard_objections_responded: "Je kreeg {count} bezwaren, reageerde op {responded} (gedetecteerd)",
      scorecard_opportunities_detected: "Kansen gedetecteerd: {count}",
      talktime_self_prospect_label: "Talk-time (self/prospect)",
      monologue_warnings_label: "Monologue warnings",
      markdown_none: "None",
    },
  };

  // ---------------------------------------------------------------------
  // fetch() shim
  // ---------------------------------------------------------------------
  const jsonResponse = (body) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  window.fetch = (input) => {
    const url = typeof input === "string" ? input : (input && input.url) || "";

    if (url.endsWith("/api/config")) {
      return Promise.resolve(
        jsonResponse({ language: ACTIVE_LANG, consent_tier: "audit", ws_host: null, ws_port: null }),
      );
    }
    if (url.endsWith("i18n/en.json")) {
      return Promise.resolve(jsonResponse(EN_CATALOG));
    }
    if (url.endsWith("i18n/nl.json")) {
      // Selected whenever ?lang=nl is set (js/i18n.js reads config.language
      // from /api/config above); real Dutch chrome catalog either way.
      return Promise.resolve(jsonResponse(NL_CATALOG));
    }
    if (url.endsWith("/api/v1/license/features")) {
      // Pro tier so the demo shows the full product (curated objection/case
      // suggestions, Slide Injector toggle, script-tracking panel) instead of
      // the Free upgrade banner and locked-teaser text.
      return Promise.resolve(
        jsonResponse({
          tier: "pro",
          features: [
            "audio.calltap",
            "presentation.dynamic_slides",
            "coaching.script_tracking",
            "response_playbook",
          ],
        }),
      );
    }
    if (url.endsWith("/api/v1/license/status")) {
      return Promise.resolve(jsonResponse({ status: "active", tier: "pro" }));
    }
    if (url.endsWith("/api/v1/auth/session-token")) {
      return Promise.resolve(jsonResponse({ token: "demo-session-token" }));
    }

    // Everything else (presets, llm-models, upload, start-call, sample-aha, ...)
    // is not needed to replay the demo: fail exactly like it would with no
    // backend running. Every caller in the real dashboard code already
    // handles a failed fetch gracefully (try/catch with a sane fallback).
    return Promise.reject(new TypeError("Failed to fetch (demo: no backend, path not stubbed): " + url));
  };

  // ---------------------------------------------------------------------
  // WebSocket shim
  // ---------------------------------------------------------------------
  // Every dashboard module does `new WebSocket(".../ws/<channel>")` at load
  // time and wires addEventListener("open"/"message"/"close"/"error"). This
  // fake socket mimics that surface; demo-timeline.js (loaded after every
  // module has connected) pushes scripted messages into it by channel name
  // via window.__demoEmit(channel, payload).
  const registry = new Map(); // channel name -> Set<FakeWebSocket>

  class FakeWebSocket extends EventTarget {
    constructor(url) {
      super();
      this.url = String(url);
      this.readyState = FakeWebSocket.CONNECTING;
      this.protocol = "";
      this.extensions = "";
      this.bufferedAmount = 0;

      const match = this.url.match(/\/ws\/([^/?#]+)/);
      this._channel = match ? match[1] : "";
      if (!registry.has(this._channel)) {
        registry.set(this._channel, new Set());
      }
      registry.get(this._channel).add(this);

      // Real WebSocket connections open asynchronously; keep that shape so
      // module-level "open" handlers (retry-count resets, connection-status
      // pills) run the same way they would against a live hub.
      this._openTimer = setTimeout(() => {
        if (this.readyState !== FakeWebSocket.CONNECTING) {
          return;
        }
        this.readyState = FakeWebSocket.OPEN;
        this.dispatchEvent(new Event("open"));
      }, 40);
    }

    send() {
      // No-op: the demo has no backend to receive client -> server sends
      // (phase clicks, swap-speakers, etc. all POST over the fetch shim
      // instead, which already fails closed harmlessly).
    }

    close() {
      if (this.readyState === FakeWebSocket.CLOSED) {
        return;
      }
      clearTimeout(this._openTimer);
      this.readyState = FakeWebSocket.CLOSED;
      const set = registry.get(this._channel);
      if (set) {
        set.delete(this);
      }
      this.dispatchEvent(new Event("close"));
    }
  }
  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2;
  FakeWebSocket.CLOSED = 3;

  window.WebSocket = FakeWebSocket;

  /** Broadcast `payload` to every fake socket currently open on `channel` (e.g. "transcript", "pain-points"). */
  const broadcast = (channel, payload) => {
    const set = registry.get(channel);
    if (!set || set.size === 0) {
      return;
    }
    const data = JSON.stringify(payload);
    set.forEach((socket) => {
      if (socket.readyState !== FakeWebSocket.OPEN) {
        return;
      }
      socket.dispatchEvent(new MessageEvent("message", { data }));
    });
  };

  /* ---- detector status ----
   * The real dashboard renders the detector-status strip from counters the
   * detector publishes about itself. The demo has no detector, so the same
   * counters are derived here from what the timeline actually pushes through
   * this shim. That keeps the strip truthful about what is on screen instead
   * of replaying hand-written numbers that would drift from the timeline.
   */
  const detectorCounts = {
    received: 0,
    skipped_not_prospect: 0,
    buffered_below_min: 0,
    debounced: 0,
    classified: 0,
    dropped_none: 0,
    dropped_low_confidence: 0,
    dispatched: 0,
  };
  const detectorStartedAt = Date.now();
  const DETECTOR_STATUS_INTERVAL_MS = 4000;

  const publishDetectorStatus = () => {
    broadcast("detector-status", {
      type: "detector_status",
      state: detectorCounts.received > 0 ? "active" : "listening",
      counts: { ...detectorCounts },
      uptime_ms: Date.now() - detectorStartedAt,
      last_chunk_age_ms: null,
      interval_ms: DETECTOR_STATUS_INTERVAL_MS,
      thresholds: { low: 0.5, high: 0.85 },
      policy: {
        only_classify_prospect: true,
        min_chunks_to_classify: 2,
        classification_debounce_seconds: 8,
      },
      timestamp_ms: Date.now(),
    });
  };

  const trackDetectorCounters = (channel, payload) => {
    if (channel === "transcript" && payload?.type === "transcript") {
      if (payload.speaker === "prospect") {
        detectorCounts.received += 1;
        detectorCounts.classified += 1;
      } else {
        detectorCounts.received += 1;
        detectorCounts.skipped_not_prospect += 1;
      }
      return;
    }
    if (
      (channel === "pain-points" && payload?.type === "pain_point")
      || (channel === "objections" && payload?.type === "objection")
      || (channel === "buying-signals" && payload?.type === "buying_signal")
    ) {
      detectorCounts.dispatched += 1;
    }
  };

  window.__demoEmit = (channel, payload) => {
    trackDetectorCounters(channel, payload);
    broadcast(channel, payload);
  };

  // Dashboard modules connect their fake sockets asynchronously (see
  // FakeWebSocket's open tick), so the first publish waits for them.
  setTimeout(publishDetectorStatus, 250);
  setInterval(publishDetectorStatus, DETECTOR_STATUS_INTERVAL_MS);
})();
