# Changelog fragments

A PR never edits `CHANGELOG.md`. It adds one file per change here instead, so parallel PRs
do not conflict. The release step assembles the fragments into `CHANGELOG.md` and deletes them.

## Format

- Path: `changelog.d/<slug>.<section>.md`
- `slug`: lowercase letters, digits and hyphens (for example `whisper-model-alias-table`).
- `section`: one of `added`, `changed`, `fixed`, `security`, `removed`, `documentation`.
- Content: Markdown list items only. Each item starts with `- ` at column 0; continuation
  lines are indented by two spaces. No headings, and the file is not empty.
- This `README.md` is never assembled.

Example, `changelog.d/recording-autostop.added.md`:

```markdown
- A recording stops by itself when both tracks have been quiet for a while.
  Set `RECORDER_AUTOSTOP_SILENCE_SECONDS` to change the wait.
```

## Commands

```bash
python scripts/changelog_assemble.py --check
python scripts/changelog_assemble.py --version 1.0.0 --date 2026-11-18 --dry-run
python scripts/changelog_assemble.py --version 1.0.0 --date 2026-11-18
```

`--check` validates every fragment. A real assemble run adds the items to `## [<version>]`
(created below `## [Unreleased]` when missing, otherwise appended to the matching
`### <Section>`, ordered by fragment filename) and deletes the fragments it used.
