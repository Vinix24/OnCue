# ADR-002: One timeline for a recording, with pause and markers

Status: Accepted

Date: 2026-11-11

## Context

A customer puts you on hold, or you confer internally. Today you stop and start again and end up with two sessions and two transcripts.

You also want to find a commitment or a price again after the call.

In `src/sales_copilot/audio/recorder_engine.py` the timeline rests on one assumption: wall time equals recording time.

- A track that starts late gets leading silence computed from the wall clock, in `_retry_failed_streams` (`now - self._start_monotonic`).
- The end of every track is padded to the elapsed wall time in `_pad_tracks_to`.

Both stop being correct once pausing exists.

## Decision

1. **Pause cuts.** During a pause the engine writes nothing. Frames are still read, so the meters keep moving, and they are discarded.

2. **One clock: recording time.** Recording time is wall time since the start minus the sum of all pauses. That one clock is used for:
   - admitting frames;
   - the leading silence of a track that starts late, also when that happens during or after a pause;
   - the end padding in `_pad_tracks_to`;
   - the time of a marker;
   - the time shown in the UI.

3. **A marker set during a pause** gets the moment the pause started.

4. **New fields in `metadata.json`**, in milliseconds of recording time. `markers` is a list of `{"offset_ms": <int>}`. `pauses` is a list of `{"offset_ms": <int>, "wall_duration_ms": <int>}`, where `offset_ms` is the recording time at which the pause began.

   ```json
   {
     "markers": [
       {"offset_ms": 754000},
       {"offset_ms": 1203500}
     ],
     "pauses": [
       {"offset_ms": 612000, "wall_duration_ms": 95000}
     ]
   }
   ```

5. **Fallback for old sessions.** When the fields are missing, as in every existing session, everything reads the session as today: no markers, no pauses.

6. **Transcript.** `render_transcript_md` in `scripts/record_server.py` writes a marker as its own line between the segments, at the right place. For example "Markering 12:34" in Dutch and "Marker 12:34" in English.

7. **Websocket actions** `pause`, `resume` and `mark`. The `state` message gains `paused` and `markers`. `GET /status` gains `paused`. An action that does not fit the current phase is ignored and logged.

## Alternatives considered

**Writing silence during a pause**, so file length keeps matching wall time. It keeps the current clock and needs no new metadata. But a ten-minute hold becomes ten minutes of silence in the WAV, which the transcriber has to chew through and which hides where the real conversation ran. Cutting is cheaper and the `pauses` list keeps the wall-clock story recoverable.

**Stop and start a new session**, as today. It needs no engine change. It leaves two sessions, two transcripts and no single place to find a commitment, which is the problem this record exists to solve.

**A clock per track.** Each track would keep its own pause bookkeeping. The two tracks could then drift apart by a frame or two per pause, and every consumer would have to reconcile them. One shared clock keeps all tracks the same length by construction.

## Consequences

- WAV length no longer equals wall duration. Times in the transcript are recording time.
- Readers of `metadata.json` must accept the optional `markers` and `pauses` fields.
- The engine change needs tests for:
  - equal track length after pause and resume;
  - no pause audio in the WAV;
  - a track that recovers during a pause landing at the right offset;
  - a marker set during a pause;
  - an old session without the new fields reading as today.
