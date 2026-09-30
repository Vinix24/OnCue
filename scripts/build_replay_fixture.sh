#!/bin/bash
# Renders a V:/P: dialogue script into a replay session directory: self.wav (V,
# "Xander") and prospect.wav (P, "Ellen"), 16 kHz mono, on a shared timeline.
# Wherever one speaker talks, the other track holds silence of the same duration.
#
#   bash scripts/build_replay_fixture.sh <dialogue.txt> <output_dir> [pause_s]
#
# <dialogue.txt> is a plain-text script with one line per turn:
#   V: <line for the rep, spoken with the "Xander" macOS voice>
#   P: <line for the prospect, spoken with the "Ellen" macOS voice>
# Blank lines and lines starting with # are ignored.
#
# Requires macOS `say`, `ffmpeg`, and `ffprobe`.
set -euo pipefail
DIALOGUE="$1"; OUT="$2"; PAUSE="${3:-1.2}"
mkdir -p "$OUT/_work"; W="$OUT/_work"
: > "$W/self.txt"; : > "$W/prospect.txt"
ffmpeg -loglevel error -y -f lavfi -i anullsrc=r=16000:cl=mono -t "$PAUSE" -c:a pcm_s16le "$W/pause.wav"
n=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  speaker="${line%%:*}"; text="${line#*:}"; text="${text# }"
  case "$speaker" in V) voice=Xander; onto=self; other=prospect ;; P) voice=Ellen; onto=prospect; other=self ;; *) continue ;; esac
  n=$((n+1)); f=$(printf '%s/%03d' "$W" "$n")
  say -v "$voice" -o "$f.aiff" "$text"
  ffmpeg -loglevel error -y -i "$f.aiff" -ar 16000 -ac 1 -c:a pcm_s16le "$f.wav"
  d=$(ffprobe -loglevel error -show_entries format=duration -of csv=p=0 "$f.wav")
  ffmpeg -loglevel error -y -f lavfi -i anullsrc=r=16000:cl=mono -t "$d" -c:a pcm_s16le "$f.silence.wav"
  printf "file '%s'\nfile '%s'\n" "$f.wav" "$W/pause.wav" >> "$W/$onto.txt"
  printf "file '%s'\nfile '%s'\n" "$f.silence.wav" "$W/pause.wav" >> "$W/$other.txt"
done < "$DIALOGUE"
for k in self prospect; do
  ffmpeg -loglevel error -y -f concat -safe 0 -i "$W/$k.txt" -ar 16000 -ac 1 -c:a pcm_s16le "$OUT/$k.wav"
done
echo "lines: $n"
for k in self prospect; do printf '%s: %ss\n' "$k" "$(ffprobe -loglevel error -show_entries format=duration -of csv=p=0 "$OUT/$k.wav")"; done
