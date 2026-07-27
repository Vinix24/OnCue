# Contributing

Thanks for your interest in contributing to OnCue.

## 1) Development Setup

1. Fork the repository and clone your fork.
2. Run the project setup script:

```bash
./scripts/setup.sh
```

3. Copy environment values:

```bash
cp .env.example .env
```

4. Start the app locally and verify baseline behavior.

## 2) Development Standards

- Follow existing project conventions described in `docs/TTD.md` and `docs/PRD.md`.
- Keep changes focused and reviewable.
- Prefer explicit, readable code over clever shortcuts.

## 3) Commit Convention

This project uses Conventional Commits:

- `feat(scope): ...`
- `fix(scope): ...`
- `refactor(scope): ...`
- `test(scope): ...`
- `docs(scope): ...`

Examples:
- `feat(detector): add objection response templates`
- `fix(transcriber): handle websocket reconnect timeout`

## 4) Pull Request Process

1. Create a branch from `main`.
2. Implement your change with tests and docs (if needed).
3. Run quality checks locally:

```bash
ruff check src/
python -m pytest tests/ -v
```

4. Push your branch and open a PR against `main`.
5. Use a clear PR description with scope, rationale, and test evidence.
6. All PRs must pass CI before merge.

## 5) Testing Expectations

All PRs must pass:

- `ruff check src/`
- `python -m pytest tests/ -v`

If your change affects behavior, include or update tests in the same PR.

If you modify dashboard or presentation JS/CSS, run `bash scripts/start.sh` (or `python scripts/bust_cache.py`) so local asset URLs refresh with the current version query parameter.

## 6) Contributor License Agreement (CLA)

All contributors must agree to the CLA in `CLA.md`.

Why this exists:
- The project is open-source under AGPL-3.0.
- The project also offers commercial licensing.
- The CLA allows maintainers to continue supporting both licensing paths.

Add this sign-off to your PR description:

```text
Signed-off-by: <Your Name> <your@email.com>
I have read and agree to the CLA in CLA.md.
```

## 7) Code of Conduct

This project follows the Contributor Covenant in `CODE_OF_CONDUCT.md`.

## 8) Reporting Security Issues

Do not open public issues for vulnerabilities.

Report privately via: `info@vincentvandeth.nl`

See `SECURITY.md` for policy details.

## 9) Need Help?

- Open a GitHub Discussion: https://github.com/Vinix24/OnCue/discussions
- Or open an issue for non-sensitive technical questions.

## Help wanted / good first areas

These are real, concrete areas where a contribution moves the project forward. None of them require access to anything proprietary — all local or BYO-credential.

- **Windows real-hardware validation** of the WASAPI capture path (`src/sales_copilot/audio/wasapi.py`). It's verified in a VM only so far — a report from a real Windows PC in a live call (headset/speakers, meeting app, what worked and what didn't) is the single most useful thing a Windows user can contribute. See the [Platform support section](README.md#platform-support) in the README.
- **Local ASR hardware sweep**: run `scripts/benchmark_transcription.py` on strong local hardware (a machine with a discrete GPU, a DGX-class workstation, or a Strix Halo laptop) and open an issue with the hardware specification, the `--threads` setting you used, and the measured p50/p95 streaming latency. Transcription is the heaviest fixed cost in the pipeline and on Apple Silicon more CPU threads do not improve throughput, so the open question is whether significantly stronger hardware closes the gap to cloud ASR while keeping audio on-device. There is currently only one datapoint (a Mac) — external measurements are the most useful contribution a non-Mac user can make right now.
- **Local LLM**: Qwen 3.6 32B-A3B, or Ollama models such as `qwen2.5:7b`, `llama3.2`, `mistral:7b` — benchmarking, prompt tuning, and provider-adapter work for any of these.
- **BYO-tenant cloud**: Azure OpenAI and Vertex/Bedrock integrations tested against a contributor's own tenant and credentials.
- **Dutch-language quality**: Gemini-Live and Groq-EU for improving Dutch transcription and detection accuracy.
- **Groq models**: `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `qwen2.5-32b`, and ASR via `whisper-large-v3-turbo`.

If you pick up one of these, open an issue first to say what you're trying, so effort doesn't collide with someone else's.
