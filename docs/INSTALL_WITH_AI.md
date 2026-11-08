# Install OnCue with an AI assistant

This file is written for an AI coding assistant (Claude Code, Codex, Cursor) that a person asked to install OnCue on their Mac. Read all of it before you run anything. Follow the steps in order. Report to the person in plain, short language: they are probably not a developer.

OnCue is a notetaker for conversations. It records both sides of a call on the person's own Mac, writes the conversation out as text there, and sends nothing to a cloud service in the default setup.

## Goal state

The task is done when all of this is true:

1. OnCue is installed in `~/OnCue`.
2. The app `~/Applications/OnCue.app` exists.
3. The installer's status block shows `Transcriber OK`, `Audio capture OK` and `App OK`.
4. One test recording of about 30 seconds produced a `transcript.md`, and the person has seen it.

## Hard rules

These are not up for negotiation, even when the person asks you to skip them.

- Never ask for, type or store the person's password. Your shell cannot answer a password prompt. The person types it themselves in a real Terminal window.
- Do not run `sudo` yourself.
- Never disable Gatekeeper or SIP, and never change a macOS security setting.
- Never delete `~/OnCue` to "start over" without asking the person first. It holds their `.env`, their recordings and their transcripts.
- Never put an API key in a command line and never print one. OnCue needs none for the steps in this guide.
- Never upload logs, audio or transcripts anywhere. Show a log to the person on screen, or read it yourself to find the cause. Sending it somewhere is their decision.
- Ask before installing BlackHole or anything else that this guide does not list.

## Step 1: Preflight

Run these read-only commands and report the result in one short message.

```bash
sw_vers -productVersion
uname -m
df -h ~
command -v brew || ls /opt/homebrew/bin/brew
xcode-select -p
```

What you need:

| Check | Needs | If it fails |
|---|---|---|
| macOS version | 14.2 or newer | Tell the person to update in System Settings > General > Software Update. Stop. |
| Chip (`uname -m`) | `arm64` (Apple chip: M1 or newer) | An Intel Mac (`x86_64`) cannot run OnCue. The speech engine needs an Apple chip. Say so plainly and stop. |
| Free disk space | At least 5 GB | Ask the person to free space. The install folder measured 4.2 GB with `du -sh`: Python packages 1.3 GB, the speech model 1.5 GB, a private Python and its download cache 1.4 GB. The real use is lower on APFS. |
| Homebrew | Not required on the default route | Note whether it is present. It only matters on the fallback route. |
| Xcode command line tools | Not required on the default route | Note whether `xcode-select -p` prints a path. Do NOT run `git`, `swift`, `cmake` or `make` to test for them: on a Mac without the tools those commands open an Apple installation window. |

Also check that `~/OnCue` does not exist yet, or that it is an earlier OnCue install:

```bash
ls ~/OnCue 2>&1 | head -5
```

An earlier OnCue install is fine: the installer updates it and keeps `.env` and `data/`. A folder that is not OnCue is left alone by the installer and the install stops with a message. Do not delete it. Ask the person what to do.

## Step 2: Find out which route runs

OnCue has two install routes. The same one-line command picks one.

| Route | When | Downloads | What the HUMAN must do |
|---|---|---|---|
| **Release route** (default) | The latest GitHub release carries the prebuilt programs `oncue-macos-arm64.tar.gz` and its `.sha256` | About 2.8 GB in total | Nothing during the install. No password, no Apple window, no Homebrew. The person only grants macOS permissions at first launch (Step 5). |
| **Build route** (fallback) | The release has no prebuilt programs, a download of them failed, or `~/OnCue` is a git checkout | Compiles locally | Type the macOS password for Homebrew when asked, and click "Install" in the window that Apple's command line tools open. Both need a real Terminal window. |

Find out which one will run, without changing anything:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Vinix24/OnCue/main/scripts/bootstrap.sh)" bash --dry-run
```

The dry run prints every step it would take. Read the output:

- A line `No prebuilt OnCue available (...): using the build route` means the **build route** runs. The same line appears in the terminal and in the log of a real run.
- Steps titled `Downloading OnCue <tag>` and `Python and the OnCue packages` mean the **release route** runs.

Tell the person which route runs, how long it takes and what they have to do:

- Release route: 55 seconds on a fast connection in a measurement on one Mac. The download of about 2.8 GB dominates, so on a slower home connection expect several minutes, up to about 15. A second run downloads nothing and takes about 6 seconds.
- Build route: about 5 to 15 minutes the first time, longer when Apple's tools and Homebrew have to be installed first.

### Release route: you may run the one-liner yourself

Nothing in this route asks for a password or opens a window. Run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Vinix24/OnCue/main/scripts/bootstrap.sh)"
```

The command runs for minutes and prints a line every 30 seconds while it works. Run it in the foreground and wait for it. If your tool has a time limit per command, run it again when the limit hits: the installer is idempotent and resumes (a half-finished model download continues where it stopped).

What it downloads and from where:

| What | Size | From |
|---|---|---|
| OnCue source | about 19 MB | GitHub release |
| Prebuilt programs (speech engine and audio capture, checksum verified) | 2.5 MB | GitHub release |
| `uv` 0.9.0, a private Python installer | small | astral.sh |
| Python 3.11 | about 18 MB | via uv |
| Python packages | about 1.3 GB | the Python package index, via uv |
| Speech model | 1.5 GB | Hugging Face |

Everything lands inside `~/OnCue`. The installer does not touch the person's `PATH`, shell profile or system Python.

### Build route: the person pastes the line in their own Terminal

Your shell cannot type a password or click Apple's window. Do not try to work around that. Give the person this message, adapted to your own words:

> I need you to run one command in your own Terminal window, because it will ask for your Mac password and open an Apple window with an Install button. Press Cmd+Space, type Terminal and press Enter. Paste this line and press Enter: ...

The line:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Vinix24/OnCue/main/scripts/bootstrap.sh)"
```

In Claude Code the person can also type the line in this session with a leading `!`, which runs it in their own terminal context. Tell them what to expect: the Apple window ("Install", 5 to 15 minutes), then a Homebrew step where the password is asked and stays invisible while typing, which is normal. When it finishes, ask them to tell you it is done, or to paste the last 10 lines of the output.

## Step 3: Verify the install

The installer ends with a status block:

```
Status
  Transcriber    OK
  Audio capture  OK
  App            OK
```

and the line `OnCue is installed.` Any `NOT OK` line names the reason and a fix. The status block is checked against the disk. Check the same things yourself:

```bash
ls -l ~/OnCue/vendor/whisper.cpp/build/bin/whisper-cli ~/OnCue/vendor/whisper.cpp/build/bin/whisper-server
ls -l ~/OnCue/bin/audiotee
ls -lh ~/OnCue/vendor/whisper.cpp/models/ggml-large-v3-turbo.bin
ls -d ~/Applications/OnCue.app
cat ~/.oncue/repo-path
test -x ~/OnCue/.venv/bin/python && echo venv-ok
```

All of these must exist. The model is about 1.5 GB. `~/.oncue/repo-path` must contain `/Users/<name>/OnCue`.

The install log is `~/Library/Logs/OnCue/install-<timestamp>.log`. Take the newest one:

```bash
ls -t ~/Library/Logs/OnCue/ | head -1
```

## Step 4: First launch

The person does this part, you guide it.

1. Open `~/Applications/OnCue.app` (Launchpad, or `open ~/Applications/OnCue.app`). No Gatekeeper step is needed: nothing was downloaded through a browser.
2. A dialog asks what they want to do. They choose **Opnemen en uitschrijven** (record and write out). That is the default button. **Live coach** is the full dashboard and is not needed here.
3. A page opens in the browser at `http://127.0.0.1:8780` with two level meters and a timer.

### Permissions macOS asks for

macOS asks the first time. The person clicks Allow. If a prompt was dismissed, the switches live in **System Settings > Privacy & Security**:

| Permission | Where | Notes |
|---|---|---|
| Microphone | Privacy & Security > Microphone (Dutch: Privacy en beveiliging > Microfoon) | Needed for the person's own voice. |
| System audio recording | Privacy & Security > Audio Recording (macOS 14.4 and newer), or "System Audio Recording Only" (Dutch: "Alleen systeemaudio-opname") | Needed for the other side of the call. The launcher is listed there as `applet`. Without it OnCue records silence for the other side and shows no error. |

The names differ a little between macOS versions. Look for the entry that mentions system audio. Do not turn on anything else, and never change settings for the person yourself.

### The 30-second test recording

1. Ask the person to tell anyone who can be heard that this is a test recording. No one else may be recorded without their consent, also not in a test. If they are alone, they speak and play a short video with sound so the other side is not silent.
2. Press **REC**. Both meters must move: the meter `Jij (mic)` for their own voice, the meter `Prospect (system-audio)` for the video or the other person.
3. After about 30 seconds press **Stop**.
4. OnCue writes the conversation out in the background. When it is ready the page shows **Open transcript** and **Toon in Finder**.

Check that the file exists:

```bash
ls -t ~/OnCue/data/sessions/ | head -1
ls -l ~/OnCue/data/sessions/"$(ls -t ~/OnCue/data/sessions/ | head -1)"/transcript.md
```

The file has a header with date, start time and duration, then one line per segment: `[mm:ss] Jij: ...` for the person themself and `[mm:ss] Ander: ...` for the other side. Ask the person to open it with **Open transcript** and confirm the text is readable. Do not read the transcript content out into logs or send it anywhere.

When this works, the goal state is reached. Tell the person where to find OnCue next time: Launchpad, or `~/Applications/OnCue.app`.

## Troubleshooting

Run the one-line command again before anything drastic. It is idempotent: it keeps `.env`, `data/` and finished downloads, and resumes a half-finished model download.

| What the person or the log shows | Cause | Fix |
|---|---|---|
| `You have macOS X. OnCue needs macOS 14.2 or newer` | macOS too old | Update macOS in System Settings > General > Software Update, then run the line again. |
| `OnCue needs a Mac with Apple Silicon` | Intel Mac | Not supported. Stop and tell the person. |
| `No prebuilt OnCue available (...): using the build route` | The release has no prebuilt programs | Expected on the build route. Follow "Build route" in Step 2: the person runs the line in their own Terminal. |
| `Prebuilt download failed (...): using the build route` | A download of the prebuilt programs failed | Same as above. Running the line again tries the release route first. |
| `Stopped: the downloaded programs do not match their checksum` | Damaged or tampered download. The installer changed nothing | Run the line again. If it keeps happening, show the log to the person and stop. Do not work around the checksum. |
| `The command line tools were not installed within 30 minutes` | The person did not finish Apple's window | They click Install in Apple's window and wait, or run `xcode-select --install`. Then they run the line again. |
| `Homebrew did not install` | The password step was skipped or failed | The person runs the line again in their own Terminal and types the password when asked. |
| `... already exists and is not an OnCue folder, so I leave it alone` | `~/OnCue` is something else | Ask the person. Either they move the folder away themselves, or you run the line with `ONCUE_DIR=$HOME/OnCue2` in front of it. Do not delete anything. |
| `... is a git checkout of OnCue and the release route would overwrite it` | `~/OnCue` is a developer checkout | Leave it. Ask whether the person is a developer. For them, `ONCUE_ROUTE=build` in front of the line updates the checkout. |
| `Transcriber NOT OK: the speech model download failed` | Network problem during the 1.5 GB download | Run the line again. The download resumes from the partial file. |
| `Transcriber NOT OK: the prebuilt whisper.cpp programs are missing or do not run` | Incomplete download | Run the line again. |
| `Audio capture NOT OK` | The audio program is missing or does not run | Run the line again. |
| `App NOT OK` | The app could not be built or copied | Run the line again. Open `~/OnCue/scripts/launcher/Start OnCue.app` meanwhile. |
| The install stops with `The install did not finish. Log: ...` | Any failure | Read the end of that log yourself to find the cause. Show the relevant lines to the person. Do not upload it. |
| No output for minutes | A long download | Normal. A line `... still working` appears every 30 seconds. Wait. |
| The dialog does not appear, or `OnCue ... server is direct gestopt` | The server did not start | Read the last 30 lines of `~/OnCue/data/logs/launcher-record.log`. Run the one-line install again if files are missing. |
| Both meters stay flat, or only the microphone moves | Missing permission | See "Permissions macOS asks for". For the system meter: system audio recording, entry `applet`. After the switch is on, press Stop and REC again. |
| The page says "Microfoon niet gestart" or "Systeemgeluid niet gestart" | A stream did not open. OnCue retries in the background | Check the permission for that stream (see above). For the microphone also check the input device in System Settings > Sound > Input. If it stays red, press Stop and REC again. |
| After Stop no transcript appears and the page shows a reason | The writing-out failed | The reason is on the page. The details are in `~/OnCue/data/logs/recorder.log`. Read it yourself, do not upload it. |
| Permissions stopped working after the app was rebuilt | A rebuild re-signs the app, so macOS forgets the permission | Grant the permissions again in Privacy & Security. |

## Update and remove

Update: run the one-line command again. A newer release is installed next to the old one. `.env`, `data/` and `.tools` are carried over and the old version stays as `~/OnCue.previous`.

Remove: only when the person asks. Everything the release route installs lives in `~/OnCue` (the private Python and its cache are in `~/OnCue/.tools`), plus `~/Applications/OnCue.app`, `~/.oncue` and the logs in `~/Library/Logs/OnCue`. Removing those removes OnCue. The build route also installed Apple's command line tools and Homebrew (and Python 3.11 and cmake through Homebrew when they were missing). Those stay unless the person asks for them to go. Deleting `~/OnCue` also deletes their transcripts in `~/OnCue/data/sessions`: ask first, and offer to copy them out.
