# Windows test plan: the one-command installer

This is the protocol for the first run of `scripts/install.ps1` on a real Windows machine. It covers the preflight, a notetaker install, starting OnCue, a two-minute test conversation, the transcript check, stopping, and `-Uninstall`.

Windows stays labelled experimental in [INSTALL.md](../INSTALL.md#windows) until this run passes. The pass criteria are at the end.

**What you need**

- A personal Windows 10 (version 2004 or newer) or Windows 11 machine, logged in with a normal user account. Administrator rights are not needed and not used.
- A headset. Speakers feed the test audio back into the microphone.
- An internet connection. The install downloads about 1.5 GB.
- About an hour. The install itself takes 15 to 30 minutes, depending on the connection.

**No customer data.** The test conversation is a public video (for example a Dutch news item on YouTube) or a Teams test call with yourself. Nobody else's voice is recorded unless they agreed to it.

**Keep notes as you go.** Section 8 lists what to send back. Copy console output with the mouse (select, then Enter or right-click) or take screenshots.

---

## 0. Before you start

1. Write down whether Python was already installed: **Settings > Apps > Installed apps**, search for "Python". You need this for the cleanup in step 7.
2. Put OnCue on the machine, in a short folder **outside OneDrive**: `C:\Users\<you>\OnCue`.
   - From a ZIP: download it, right-click > **Extract All...**, and choose `C:\Users\<you>` as the target. Rename the extracted folder to `OnCue` if it has another name.
   - With git: `git clone <repository URL> $env:USERPROFILE\OnCue`
3. Check that `C:\Users\<you>\OnCue` contains `pyproject.toml` and `scripts\install.ps1` directly, not one folder deeper.
4. Open **Windows PowerShell** from the Start menu. Do not pick "Run as administrator".
5. Go to the folder:

   ```powershell
   cd $env:USERPROFILE\OnCue
   ```

## 1. Preflight

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -PreflightOnly
```

**Expected output** (your values differ; a Dutch Windows shows a decimal comma):

```
OnCue for Windows: preflight (changes nothing)
  OK    PowerShell 5.1.26100.xxxx
  OK    Microsoft Windows 11 Home 24H2 (build 26100)
  OK    Architecture: x64
  OK    CPU: <processor name>, 8 cores, 16 threads -> WHISPER_CPP_THREADS=7
  OK    RAM: 16,0 GB
  OK    Free disk space on C:\: 120,4 GB (needs about 3,3 GB for notetaker + large-v3-turbo-q8_0)
  OK    Python 3.13.x (64-bit): C:\Users\<you>\AppData\Local\Programs\Python\Python313\python.exe
  OK    pypi.org (Python packages): reachable (HTTP 200)
  OK    github.com (whisper.cpp v1.9.1): reachable (HTTP 200)
  OK    huggingface.co (model large-v3-turbo-q8_0): reachable (HTTP 200)

Preflight: 10 OK, 0 warning(s), 0 blocking.
```

**Check that the preflight changed nothing:**

```powershell
Test-Path .oncue-win, .venv, .env
```

Expected: `False`, `False`, `False`.

**When Python is missing**, the preflight shows `FAIL  Python 3.11 to 3.14 (64-bit) not found` with an install command. Run that command:

```powershell
winget install -e --id Python.Python.3.13 --scope user
```

Then close PowerShell, open a new one, `cd $env:USERPROFILE\OnCue` again, and repeat the preflight. Note that you installed Python for this test.

**When something else shows FAIL or WARN**, note the line. A `WARN` does not stop the install. A `FAIL` does: stop here and report it.

A `WARN  Microsoft Visual C++ runtime not found` line is worth noting in particular. Continue anyway: step 2 shows whether whisper.cpp needs that runtime or brings its own files.

## 2. Install the notetaker profile

Write down the start time, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
```

**Expected output**, after the same preflight block as in step 1:

```
Python environment (.venv) and packages, profile notetaker
        Creating .venv with Python 3.13.x (C:\...\python.exe) ...
  OK    .venv created
        Updating pip ...
        pip step 1 of 2: OnCue with the Windows capture extra (pip install -e .[windows])
        This step downloads PyTorch and takes several minutes on a normal connection.
          Collecting ...
          Downloading torch-...-win_amd64.whl (124.x MB)
          ...
          Successfully installed ...
        pip step 2 of 2: aiosqlite (post-call report and saved transcript) (pip install aiosqlite --only-binary=:all:)
          ...
  OK    Packages installed for profile notetaker

whisper.cpp v1.9.1 (prebuilt, CPU)
        Downloading https://github.com/ggml-org/whisper.cpp/releases/download/v1.9.1/whisper-bin-x64.zip
           10%  ... of 8 MB
          ...
  OK    Downloaded whisper-bin-x64.zip, sha256 matches the digest GitHub publishes for this release asset
  OK    whisper-cli.exe: C:\Users\<you>\OnCue\.oncue-win\whisper.cpp\v1.9.1\...\whisper-cli.exe
  OK    whisper-server.exe: C:\Users\<you>\OnCue\.oncue-win\whisper.cpp\v1.9.1\...\whisper-server.exe
  OK    whisper-cli.exe starts

Model large-v3-turbo-q8_0 (834 MB)
        Downloading https://huggingface.co/ggerganov/whisper.cpp/resolve/.../ggml-large-v3-turbo-q8_0.bin
           10%  83 MB of 834 MB
          ...
  OK    Downloaded ggml-large-v3-turbo-q8_0.bin, sha256 matches the value Hugging Face publishes for revision 5359861

.env
  OK    .env created from .env.example with the Windows defaults (WHISPER_CPP_THREADS=7)

Final check
  OK    OnCue imports in .venv (...)
        Transcription self-test: whisper.cpp loads the model and transcribes 2 seconds of silence. This takes up to a minute.
  OK    Transcription self-test passed in 15 s
  OK    .env WHISPER_CPP_BINARY -> C:\Users\<you>\OnCue\.oncue-win\...\whisper-cli.exe
  OK    .env WHISPER_CPP_MODEL_PATH -> C:\Users\<you>\OnCue\.oncue-win\models\ggml-large-v3-turbo-q8_0.bin

OnCue is installed (profile notetaker, model large-v3-turbo-q8_0).

Start OnCue with this line in PowerShell:
  Set-Location -LiteralPath 'C:\Users\<you>\OnCue'; .\.venv\Scripts\python.exe -m sales_copilot
...
Install log: C:\Users\<you>\OnCue\.oncue-win\logs\install-YYYYMMDD-HHMMSS.log
```

Write down the end time and the number of seconds the self-test took.

**Watch for, and note the exact text or take a screenshot of:**

- A SmartScreen or Microsoft Defender window, or a Defender notification about a blocked or quarantined file.
- A dialog saying `MSVCP140.dll` or `VCRUNTIME140.dll` was not found. Click OK. The script then stops with "a DLL is missing". The fix is the Microsoft Visual C++ Redistributable, which needs administrator rights. That is a change to the system outside this test: stop here and report it.
- A Windows Firewall question. Choose **Cancel**. OnCue only listens on this machine itself (127.0.0.1) and needs no firewall rule.
- A red `FAIL` line. Copy it, plus the yellow "Last lines of output" block above it if there is one. Running the same command again resumes where it stopped; note whether that worked.

**Copy the install log now**, before step 7 removes it. Its path is on the last lines of the output. Put a copy on the Desktop:

```powershell
Copy-Item .oncue-win\logs\install-*.log $env:USERPROFILE\Desktop\
```

The log contains your Windows user name in file paths and nothing else personal.

## 3. Start OnCue

Copy the start line the installer printed and run it in the same PowerShell window:

```powershell
Set-Location -LiteralPath 'C:\Users\<you>\OnCue'; .\.venv\Scripts\python.exe -m sales_copilot
```

**Expected:** log lines in PowerShell, among them `OnCue v1.0.0rc3`, `WebSocket hub: ws://127.0.0.1:8760` and `Waiting for start_call config...`. The browser opens the setup wizard at `http://localhost:8760/dashboard/wizard/`.

For this test, skip the wizard and open:

```
http://localhost:8760/dashboard
```

Leave this PowerShell window open. OnCue runs as long as it is open.

## 4. Two-minute test conversation

1. Put on the headset.
2. Prepare the audio in another browser tab: a Dutch video of a few minutes, paused at the start. Or open a Teams test call (Teams > Settings > Devices > **Make a test call**).
3. In the dashboard, under **Modules**:
   - leave "Talk Time", "Live Transcript" and "Post-call Report" **on**.
   - leave "AI detectie" (in English: "AI detection") on as well. The notetaker profile does not include detection. After Start Call the dashboard shows a message that the detector module did not start, with the command `pip install ".[detector]"`. OnCue keeps running and transcribing. Note whether the message appeared and whether it was clear.
4. Click **Start Call**.
5. Play the video, or talk in the Teams test call, for two minutes. Also say a few sentences yourself.
6. Click **Stop & rapport** (in English: "Stop & report").

**Expected:**

- Within a few seconds of speech in the video, transcript lines appear in the dashboard.
- When you speak, the talk-time bar moves. Your own voice is not transcribed live by default (`TRANSCRIBE_SELF_LIVE=false`); it counts in talk time.
- After "Stop & rapport" a report appears. The PowerShell window keeps running.

**Note:** whether transcript lines appeared (yes/no), roughly how many seconds after the speech, whether the talk-time bar responded to your voice, and any red lines in the PowerShell window or error messages in the dashboard.

## 5. Check the transcript on disk

Open a **second** PowerShell window (OnCue keeps running in the first) and run:

```powershell
cd $env:USERPROFILE\OnCue
$file = Get-ChildItem data\reports\*_report.json | Sort-Object LastWriteTime | Select-Object -Last 1
$file.Name
$report = Get-Content $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$report.full_transcript.Count
$report.full_transcript | Select-Object -First 5 speaker, text
```

**Expected:** a file name like `2026-11-02T14-05-31_<session id>_report.json`, a count above zero, and five lines of text from the video.

**Note:** the file name, the count, and two of the lines. They come from a public video, so they are fine to share.

## 6. Stop OnCue

Go back to the first PowerShell window and press **Ctrl+C**.

**Expected:** `Shutdown requested. Stopping modules...` and within about ten seconds the PowerShell prompt returns.

Then open Task Manager (**Ctrl+Shift+Esc**), tab **Details**, and look for `whisper-server.exe` and `whisper-cli.exe`.

**Expected:** neither is there. **Note** anything that is still running.

## 7. Uninstall

In PowerShell, in the OnCue folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
```

**Expected output:**

```
OnCue for Windows: uninstall

These items will be removed:
  - C:\Users\<you>\OnCue\.env  [4x KB]  .env (created by this installer, unchanged since)
  - C:\Users\<you>\OnCue\.venv  [1,x GB]  .venv (Python environment created by this installer)
  - C:\Users\<you>\OnCue\.oncue-win  [8xx MB]  .oncue-win (whisper.cpp, models, install log and manifest)

These stay:
  - data\ and .vnx-data\: your sessions, reports and transcripts. OnCue writes them while it runs; the installer did not.
  - The project folder itself and Python. Remove those yourself when you are done.

Remove the items listed above? Type y and press Enter to confirm:
```

Type `y` and press Enter.

```
  OK    Removed C:\Users\<you>\OnCue\.env
  OK    Removed C:\Users\<you>\OnCue\.venv
  OK    Removed C:\Users\<you>\OnCue\.oncue-win

Outside the project folder:
  OK    not present: C:\Users\<you>\AppData\Local\SalesCopilot
  OK    not present: C:\Users\<you>\AppData\Local\pip\Cache
  OK    not present: C:\Users\<you>\.cache\huggingface\hub\models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2
  OK    not present: C:\Users\<you>\.cache\torch

Uninstall finished.
```

A line `present, but it already existed before the first install` is fine: that folder was there before the test. A `WARN  appeared after the install` line is not: note it.

**Then finish the cleanup by hand:**

1. Close PowerShell.
2. In File Explorer, delete `C:\Users\<you>\OnCue`. It still holds `data\` with the test transcript.
3. If you installed Python for this test (step 0 or 1): **Settings > Apps > Installed apps**, uninstall "Python 3.13.x (64-bit)" and, if it appears, "Python Launcher".

After this the machine is as it was before the test.

## 8. What to send back

1. **Preflight output** from step 1, complete.
2. **Install duration:** start and end time from step 2, and the self-test seconds.
3. **Error messages:** every red `FAIL` line, every "Last lines of output" block, every dialog (screenshot), and the install log you copied to the Desktop in step 2.
4. **SmartScreen, Defender, firewall:** did anything warn or block? Exact text or a screenshot. Say "nothing" if nothing appeared.
5. **Test conversation** (step 4): transcript visible live yes/no, rough delay in seconds, talk time responding to your voice yes/no.
6. **Transcript on disk** (step 5): file name, count, two sample lines.
7. **Stopping** (step 6): did the prompt return, and was anything left in Task Manager?
8. **Uninstall** (step 7): the output, in particular the "Outside the project folder" lines.
9. **Python:** already present before the test, or installed for it and removed afterwards.

## Pass criteria

The install path counts as verified on this machine when all of these hold:

- The preflight reports 0 blocking on a machine that meets the requirements, and creates no files.
- The install finishes without manual steps beyond what the preflight asked for (installing Python).
- Transcript lines appear live during the test conversation, and the report in `data\reports` holds them.
- After Ctrl+C no `whisper-server.exe` or OnCue `python.exe` keeps running.
- After `-Uninstall` and deleting the folder, nothing from OnCue remains outside the project folder: every line under "Outside the project folder" is `not present` or `already existed before the first install`.

## Quick help

| You see | Cause and what to do |
|---|---|
| `running scripts is disabled on this system` | The script was started without the prefix. Use the full line: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1`. |
| `This does not look like a complete copy of the OnCue repository` | PowerShell is in the wrong folder, or the ZIP was extracted one level deeper. See step 0, point 3. |
| `WARN  The project folder is inside OneDrive` | Move the folder to `C:\Users\<you>\OnCue` and start again at step 1. |
| `WARN  ... Windows long paths are off` | Use the short folder from step 0. |
| `FAIL ... not reachable` | No internet, a proxy, or a company network that blocks the site. Note the full line. |
| pip fails in step 2 | Note the "Last lines of output" block, then run the install line once more. It resumes. |
| The dashboard does not load | Check that the first PowerShell window still runs OnCue, and use `http://localhost:8760/dashboard`. |
| OnCue stops the moment you click Start Call | This should no longer happen. Copy the last lines of the PowerShell window and the log, and note that this happened. A missing detection package shows as a message in the dashboard, not as a stop. |
