# Security Policy

## Reporting a Vulnerability

Report privately via GitHub's private vulnerability reporting, or email
`info@vincentvandeth.nl` rather than opening a public GitHub issue.

We commit to:
- Acknowledging receipt within 48 hours
- Providing a status update within 7 days
- Disclosing publicly only after a fix is available

## Supported Versions

Only the latest major release receives security updates.

## PGP Key

(Placeholder: add a PGP public key when one is generated)

## Credential scope for worker processes

Environment variables are inherited by every descendant process. A credential exported
once — in a shell profile, a launchd session, or a long-lived `tmux` server — reaches
every build worker, test run, and agent session started underneath it, whether or not
that process has any use for it. A worker that runs `env`, an ordinary debugging step,
then copies the value into its own transcript, and transcripts are written to disk.

Two gates guard this repository:

- **`.gitleaks.toml`** rule `worker-env-credential-literal` rejects a credential-shaped
  variable carrying a literal value in a file that populates a process environment
  (`.env*`, the `env` block of `.claude/settings*.json`, `*.plist`). This runs in the
  pre-commit hook and in the public-export gate.
- **`scripts/check_env_credential_scope.py`** (CI gate) fails on the same shape in
  committed sources, and reports credential-shaped variables that are present in the
  process environment but not declared in `.env.example`. `.env.example` is this
  project's declaration of what it consumes; anything credential-shaped outside it has
  no consumer here.

The second half is advisory by default because the cause is usually outside this
repository. Run it with `--strict` to make it fatal — that is how an operator confirms
a remediation actually landed:

```bash
python scripts/check_env_credential_scope.py --strict
```

### Operator remediation

Removing an `export` from `~/.zshrc` does not clear the value from an already-running
`tmux` server: the server captured its environment when it started and hands that copy
to every new pane. Clear it at the server level and confirm:

```bash
tmux set-environment -g -u VNX_SMTP_PASS
tmux show-environment -g | grep -c VNX_SMTP_PASS   # expect 0
```

Panes that were already open keep the old value; restart them, or restart the `tmux`
server, before dispatching further work. A credential that a single script needs
belongs in a secret store that the script reads directly — on macOS, the keychain:

```bash
security find-generic-password -s <item-name> -a "$USER" -w
```
