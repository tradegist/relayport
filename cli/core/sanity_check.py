import argparse
import re
import shutil
import subprocess

from cli.core import (
    compose_invocation,
    config,
    env,
    load_env,
    ssh_cmd,
)

_LOG_TAIL = 100
_LOG_SINCE = "5m"
_CLAUDE_TIMEOUT_SECONDS = 60
_SSH_TIMEOUT_SECONDS = 30
# Hard cap on droplet output forwarded to claude. `docker compose logs --tail N`
# is per-service, so total volume scales with service count. The cap bounds LLM
# context size and limits the blast radius if a misbehaving service spams logs
# with secrets the project rules say must never be logged.
_MAX_PROMPT_CHARS = 50_000
# Skip values shorter than this when collecting secrets to redact — even with
# name-based filtering, a 1-char "secret" would match too much in logs.
_MIN_SECRET_LEN = 4
# Names matched (case-insensitive) flag an env var as a credential whose value
# must be redacted. Plain config keys (RELAYS, NOTIFIERS, POLL_INTERVAL, ...)
# do NOT match — their values stay readable in the prompt so claude has the
# context it needs to judge service health.
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|password|key|auth|webhook|query_id|hash|credential)"
)

_VERDICT_RE = re.compile(r"^\s*\[(GREEN|YELLOW|RED)\]")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9_.\-+/=]{8,}")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*)[^\n]+")

_SUMMARIZE_PROMPT = """\
You are reviewing the output of post-deploy diagnostic commands run on a droplet.

Reply with a single short paragraph in this exact shape:
[GREEN|YELLOW|RED] <one-line reason>. <one concrete next step if not GREEN>.

GREEN  = all containers Up, no errors/exceptions/restart loops in logs.
YELLOW = warnings or one slow startup, but services are still serving.
RED    = at least one container down, in a restart loop, or producing exceptions.

No follow-up questions, no offers to help further. Stop after the verdict.

{droplet_output}
"""


def skip_post_deploy_check() -> bool:
    """Return True when ``SKIP_POST_DEPLOY_CHECK`` is set to a non-empty,
    non-"0" value.

    Fail-closed semantics for an opt-out flag: any non-empty value other than
    "0" means skip. ``SKIP_POST_DEPLOY_CHECK=true`` / ``yes`` / ``foo`` all
    skip — the operator explicitly set something, so honour the intent rather
    than silently running the check (which could ship logs to the LLM when the
    user meant to opt out). Only "" and "0" allow the check to run.

    The actual value is surfaced in the skip message in
    :func:`post_deploy_sanity_check` so the operator can see what triggered it.
    """
    raw = env("SKIP_POST_DEPLOY_CHECK", "").strip()
    return bool(raw) and raw != "0"


def _collect_secrets_to_redact() -> set[str]:
    """Read values from project .env files whose KEY names look sensitive.

    Filter is name-based, not value-based: only values for vars whose name
    matches :data:`_SENSITIVE_KEY_RE` (TOKEN / SECRET / PASSWORD / KEY / AUTH /
    WEBHOOK / QUERY_ID / HASH / CREDENTIAL) are collected. Plain config like
    ``NOTIFIERS=webhook`` or ``RELAYS=ibkr`` is intentionally NOT redacted —
    those values are diagnostically useful and not credentials.
    """
    cfg = config()
    secrets: set[str] = set()
    for name in (".env", ".env.droplet", ".env.relays"):
        path = cfg.project_dir / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            # Strip optional `export ` prefix so e.g. `export DO_API_TOKEN=...`
            # is still matched on the actual var name.
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if not _SENSITIVE_KEY_RE.search(key):
                continue
            value = value.strip().strip('"').strip("'")
            if len(value) < _MIN_SECRET_LEN:
                continue
            secrets.add(value)
    return secrets


def _redact(text: str, secrets: set[str]) -> str:
    """Replace known secret values and common auth patterns with [REDACTED]."""
    # Sort longest-first so we don't leave a partial match behind when one
    # secret is a prefix of another (e.g. a token that contains a shorter URL).
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", text)
    return text


def _fetch_droplet_state(droplet_ip: str) -> str | None:
    """Run ``docker compose ps`` and bounded ``docker compose logs`` over SSH.

    Uses the same compose overlays / env vars / profiles as deploy and sync
    via :func:`compose_invocation` — otherwise shared-mode and shared-network
    services would be invisible to the sanity check and produce false RED
    verdicts.

    Returns the captured output, or None on SSH failure or timeout. The output
    is bounded by ``--tail`` / ``--since`` at the remote end and then by
    ``_MAX_PROMPT_CHARS`` in the caller; redaction runs between the two.
    """
    cfg = config()
    env_prefix, file_args = compose_invocation()
    remote_cmd = (
        f"cd {cfg.remote_dir} && "
        f"echo '=== docker compose ps ===' && "
        f"{env_prefix}docker compose {file_args}ps && "
        f"echo && echo '=== docker compose logs --since {_LOG_SINCE} --tail {_LOG_TAIL} (per service) ===' && "
        f"{env_prefix}docker compose {file_args}logs --since {_LOG_SINCE} --tail {_LOG_TAIL} --no-color"
    )
    try:
        result = ssh_cmd(
            droplet_ip,
            remote_cmd,
            capture=True,
            timeout=_SSH_TIMEOUT_SECONDS,
            # BatchMode=yes (set by timeout) makes the first-contact host-key
            # confirmation prompt fail; accept-new lets first contact succeed
            # while still rejecting *changed* keys (MITM protection intact).
            accept_new_host_keys=True,
        )
    except subprocess.TimeoutExpired:
        print(f"[sanity-check] SSH timed out after {_SSH_TIMEOUT_SECONDS}s — verify the droplet manually")
        return None
    except subprocess.CalledProcessError as e:
        # CalledProcessError fires for *any* non-zero exit — could be SSH
        # itself (auth, network), or the remote `docker compose` command.
        # We can't tell which from the exception alone, so report it
        # generically and include the first stderr line for diagnosis.
        first_err = (e.stderr or "").strip().splitlines()[:1]
        reason = first_err[0] if first_err else f"exit {e.returncode}"
        print(f"[sanity-check] droplet probe failed (SSH or remote `docker compose`): {reason}")
        return None
    except Exception as e:
        print(f"[sanity-check] skipped: {e}")
        return None
    return result.stdout


def _truncate(text: str, limit: int) -> str:
    """Truncate text so the returned string is at most ``limit`` chars total.

    When truncation happens, an explicit marker is appended and the body is
    sliced to leave room for it — the marker length counts against ``limit``
    so the total never exceeds the cap (the point of the cap: a strict upper
    bound on what gets sent to claude). If ``limit`` is too small to fit the
    marker at all (pathological case), the text is hard-truncated without a
    marker rather than emit a string longer than the limit.
    """
    if len(text) <= limit:
        return text
    marker = f"\n\n... [truncated at {limit} chars]"
    if len(marker) >= limit:
        return text[:limit]
    return text[:limit - len(marker)] + marker


def _print_verdict(output: str) -> None:
    """Print claude's verdict under a banner.

    Enforces the contract: a single line starting with ``[GREEN|YELLOW|RED]``.
    Multi-line replies are truncated to the first line (with a note), and
    unprefixed output is reported as malformed so it can't masquerade as a
    successful verdict.
    """
    if not output:
        print("[sanity-check] claude returned no output")
        return
    lines = output.splitlines()
    first_line = lines[0]
    if not _VERDICT_RE.match(first_line):
        print(f"[sanity-check] claude returned unexpected format — first line: {first_line}")
        return
    print("── Sanity check " + "─" * 44)
    print(first_line)
    if len(lines) > 1:
        print(f"[sanity-check] (claude added {len(lines) - 1} extra line(s); truncated to first)")
    print("─" * 60)


def run_sanity_check(droplet_ip: str) -> None:
    """Run the sanity check against the droplet.

    Flow:

    1. SSH to the droplet (Python, with timeout) and capture ``docker compose ps``
       plus bounded recent logs, using the same compose overlays as deploy/sync.
    2. Redact known secret values (from ``.env*`` files) and common auth
       patterns (``Bearer``, ``Authorization:``) from the captured output.
    3. Truncate to ``_MAX_PROMPT_CHARS`` and pipe the result via stdin to a
       non-interactive ``claude --print`` call.

    Claude runs with no tools — pure text-in / text-out, so there is no
    permission bypass and no agent-driven shell execution.

    Best-effort: any failure prints a one-line warning and returns.
    """
    if not shutil.which("claude"):
        print("[sanity-check] claude CLI not installed — skipping")
        return

    print("Running sanity check...")

    raw_output = _fetch_droplet_state(droplet_ip)
    if raw_output is None:
        return

    # Redaction reads `.env*` files and runs regex; an I/O or encoding error
    # must not crash a best-effort hook AND must not leak unredacted output.
    # Skip the check on redaction failure rather than send the raw payload.
    try:
        redacted = _redact(raw_output, _collect_secrets_to_redact())
    except Exception as e:
        print(f"[sanity-check] redaction failed, skipping check: {e}")
        return

    prompt = _SUMMARIZE_PROMPT.format(
        droplet_output=_truncate(redacted, _MAX_PROMPT_CHARS),
    )
    try:
        result = subprocess.run(
            [
                "claude",
                "--print",
                "--model", "claude-sonnet-4-6",
                "--max-budget-usd", "0.50",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLAUDE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"[sanity-check] timed out after {_CLAUDE_TIMEOUT_SECONDS}s — verify the droplet manually")
        return
    except Exception as e:
        print(f"[sanity-check] skipped: {e}")
        return

    if result.returncode != 0:
        first_err = (result.stderr or "").strip().splitlines()[:1]
        reason = first_err[0] if first_err else f"exit {result.returncode}"
        print(f"[sanity-check] claude exited {result.returncode}: {reason}")
        return

    _print_verdict((result.stdout or "").strip())


def post_deploy_sanity_check(droplet_ip: str, *, skip_flag: bool) -> None:
    """Post-deploy hook wrapper. Honors --skip-post-check and SKIP_POST_DEPLOY_CHECK."""
    if skip_flag:
        print("[sanity-check] skipped (--skip-post-check)")
        return
    if skip_post_deploy_check():
        raw = env("SKIP_POST_DEPLOY_CHECK", "").strip()
        print(f"[sanity-check] skipped (SKIP_POST_DEPLOY_CHECK={raw!r})")
        return
    run_sanity_check(droplet_ip)


def run(args: argparse.Namespace) -> None:
    """CLI entry point for ``python -m cli sanity-check-deployment``.

    Standalone runs ignore SKIP_POST_DEPLOY_CHECK — the operator explicitly
    asked for the check.
    """
    load_env()
    droplet_ip = env("DROPLET_IP")
    run_sanity_check(droplet_ip)
