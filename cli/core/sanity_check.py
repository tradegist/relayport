import argparse
import os
import re
import shutil
import subprocess

from cli.core import config, env, load_env, ssh_cmd

_LOG_TAIL = 100
_LOG_SINCE = "5m"
_CLAUDE_TIMEOUT_SECONDS = 60
# Hard cap on droplet output forwarded to claude. `docker compose logs --tail N`
# is per-service, so total volume scales with service count. The cap bounds LLM
# context size and limits the blast radius if a misbehaving service spams logs
# with secrets the project rules say must never be logged.
_MAX_PROMPT_CHARS = 50_000

_VERDICT_RE = re.compile(r"^\s*\[(GREEN|YELLOW|RED)\]")

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
    """Return True when SKIP_POST_DEPLOY_CHECK=1 is set in the environment."""
    return os.environ.get("SKIP_POST_DEPLOY_CHECK", "").strip() == "1"


def _fetch_droplet_state(droplet_ip: str) -> str | None:
    """Run `docker compose ps` and bounded `docker compose logs` over SSH.

    Returns the captured output, or None on SSH failure. `--tail` is per
    service, so the full output is also capped to ``_MAX_PROMPT_CHARS`` in
    the caller before being sent to claude.
    """
    cfg = config()
    remote_cmd = (
        f"cd {cfg.remote_dir} && "
        f"echo '=== docker compose ps ===' && docker compose ps && "
        f"echo && echo '=== docker compose logs --since {_LOG_SINCE} --tail {_LOG_TAIL} (per service) ===' && "
        f"docker compose logs --since {_LOG_SINCE} --tail {_LOG_TAIL} --no-color"
    )
    try:
        result = ssh_cmd(droplet_ip, remote_cmd, capture=True)
    except subprocess.CalledProcessError as e:
        first_err = (e.stderr or "").strip().splitlines()[:1]
        reason = first_err[0] if first_err else f"exit {e.returncode}"
        print(f"[sanity-check] SSH to droplet failed: {reason}")
        return None
    except Exception as e:
        print(f"[sanity-check] skipped: {e}")
        return None
    return result.stdout


def _truncate(text: str, limit: int) -> str:
    """Truncate text to ``limit`` chars, appending an explicit marker if cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated at {limit} chars]"


def _print_verdict(output: str) -> None:
    """Print claude's output under a banner; warn if it doesn't match the expected verdict shape."""
    if not output:
        print("[sanity-check] claude returned no output")
        return
    if not _VERDICT_RE.match(output):
        first_line = output.splitlines()[0]
        print(f"[sanity-check] claude returned unexpected format — first line: {first_line}")
        return
    print("── Sanity check " + "─" * 44)
    print(output)
    print("─" * 60)


def run_sanity_check(droplet_ip: str) -> None:
    """Run the sanity check against the droplet.

    Gathers ``docker compose ps`` and bounded recent logs over SSH (in Python),
    truncates to ``_MAX_PROMPT_CHARS``, then feeds the result to a
    non-interactive ``claude --print`` call (via stdin, to avoid argv size
    limits) for summarization. Claude runs with no tools — pure text-in /
    text-out, so there is no permission bypass and no risk of agent-driven
    shell execution.

    Best-effort: any failure prints a one-line warning and returns.
    """
    if not shutil.which("claude"):
        print("[sanity-check] claude CLI not installed — skipping")
        return

    droplet_output = _fetch_droplet_state(droplet_ip)
    if droplet_output is None:
        return

    prompt = _SUMMARIZE_PROMPT.format(
        droplet_output=_truncate(droplet_output, _MAX_PROMPT_CHARS),
    )

    print("Running sanity check (claude summarization)...")
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
        print("[sanity-check] skipped (SKIP_POST_DEPLOY_CHECK=1)")
        return
    run_sanity_check(droplet_ip)


def run(args: argparse.Namespace) -> None:
    """CLI entry point for `python -m cli sanity-check-deployment`.

    Standalone runs ignore SKIP_POST_DEPLOY_CHECK — the operator explicitly
    asked for the check.
    """
    load_env()
    droplet_ip = env("DROPLET_IP")
    run_sanity_check(droplet_ip)
