---
name: handle-cr-comments
description: Handle a round of code-review (CR) comments on a PR — fetch, categorize, fix, AND adversarially audit your own changes before pushing. Use when the user asks to "check the CR", "address PR comments", "validate and fix the review", pastes a GitHub PR review URL (`/pullrequestreview-N`), or otherwise points at unresolved reviewer feedback. This skill exists because reactive fixing (only addressing flagged items) lets new code introduce new edge cases that the next review round will find — be adversarial up front to break the loop.
---

# Handling a round of CR comments

The default failure mode is **reactive fixing**: read each comment, patch each item, push, repeat. Every round adds new code; new code has new edge cases; the next review finds them; the loop never ends.

This skill enforces an adversarial audit *between* the fixing and the push, so the next round (if any) is about genuinely new findings, not about edge cases you should have caught yourself.

## The flow

```
fetch comments → categorize → fix → adversarial self-audit → validate → commit + push → reply to false positives
                                              ^
                                    this is the step that's usually skipped
```

## Step 1 — Fetch the comments

Use the GitHub API directly via the user's pasted URL. For a URL like
`https://github.com/OWNER/REPO/pull/N#pullrequestreview-REVIEW_ID`:

```bash
gh api repos/OWNER/REPO/pulls/N/reviews/REVIEW_ID --jq '{state, body}'
gh api repos/OWNER/REPO/pulls/N/comments --jq '.[] | select(.pull_request_review_id == REVIEW_ID) | {path, line, body}'
```

If the user pastes URLs for multiple PRs (sibling-project mirror situation — see [the mirror rule](#mirror-rule-relayport--ibkr_bridge)), fetch all of them up front.

## Step 2 — Categorize every comment

For each comment, decide before touching code:

- **Valid bug** — real correctness or contract issue. Fix.
- **Valid design concern** — not a bug, but a defensible alternative. Fix or push back; never silently ignore.
- **Pedantic but valid** — technically correct nit (chars vs bytes, log wording precision). Fix if cheap, otherwise reply.
- **False positive** — the reviewer is wrong (e.g. claims a variable is unused when it isn't). Do not change code. Reply on the inline comment with evidence.

Run `make lint` and `make typecheck` immediately on the affected files before you trust a "this would fail X check" claim — automated reviewers hallucinate F841 / mypy errors on code that passes both.

## Step 3 — Fix each valid item

Standard editing. Keep changes minimal and scoped to the comment. **Do not bundle unrelated refactors** into a CR-response commit; if you spot something during the audit that warrants a separate change, defer it or call it out.

## Step 4 — Adversarial self-audit (THE step that breaks the loop)

After the fixes, before staging anything, re-read the **full changed file(s)** end-to-end as if you were the reviewer. Do not just diff. The previous round's reviewer already read the diff; the next round's reviewer will too. Your job here is to read the *integrated result* and find what the next pass would flag.

Run through this checklist for every changed file:

### 4a. New branches you just introduced

You almost certainly added at least one new `try/except`, `if`, `elif`, or option flag this round. For each one:

- Can the new branch crash? Does it propagate exceptions to a caller that documents "never aborts"?
- Does the new branch have an output side-effect (print, log, file write) that fires in cases it shouldn't?
- Are there parameter combinations that hit two new branches simultaneously and produce duplicated output (e.g. emitting `ConnectTimeout` twice)?
- Does the new branch's *default* path do something safe when the new param is absent?

### 4b. Contract drift

The docstring and the code must agree.

- If you reworded a docstring this round, does the code actually do what the new wording promises?
- If the doc says "best-effort, never aborts" and you added a new `raise` or unwrapped failure path, the contract is broken.
- If the doc says "single-line verdict" and `_print_verdict` prints `output` (which may have newlines), the contract is broken.
- If the doc says "50 KB" and the code caps by char count, the contract is at least imprecise.

### 4c. Messages and labels

- Does every operator-facing string accurately describe what just happened? Not "SSH failed" when the cause might be a remote command.
- Does any `print()` fire *before* a skip gate that could make it untrue (e.g. "Running sanity check..." printing then immediately skipping)?
- Does the wording promise behavior the code doesn't enforce (e.g. "always", "exactly", "never" when there's a code path that violates it)?

### 4d. Edge cases the diff doesn't show

- First-time use: what happens on a fresh droplet / first connection / first run? Does any new defensive flag (`BatchMode=yes`, `accept-new`, etc.) interact badly with first-contact?
- Empty inputs: empty `.env` file, empty claude stdout, empty stderr, empty secrets set.
- Concurrent calls: if two operators invoke this at once, does the lock/dedup/state hold?
- Backwards-compat: did you change the signature or behavior of a function with other callers? Did you update them?

### 4e. Quoting and shell injection

Any new code that builds a shell string (`f"... {var} ..."` then runs it via `ssh_cmd` / `subprocess`) — is `var` validated at its source? If it can contain `'`, `;`, or `$`, it's an injection vector.

### 4f. Leak paths

Any new code that forwards data to an external service (LLM call, webhook, log shipper) — does it go through the redaction/cap layer? Or did the new code bypass it?

## Step 5 — Validate

Run `make lint && make typecheck && make test`. Tests must stay green. If a probe is cheap (mock-based, no live SSH), add one in-line via `python -c` for the new behavior — verifying skip messages, verdict shape, etc.

## Step 6 — Commit and push

Single commit per repo, descriptive message. Format:

```
Address CR: <short summary of the changes, not the comments>

- <change 1 with the *why*>
- <change 2>
...
```

Co-author footer per project convention.

## Step 7 — Reply to false positives

For any comment you flagged as a false positive in step 2, post an inline reply via `gh api`:

```bash
gh api -X POST repos/OWNER/REPO/pulls/N/comments/COMMENT_ID/replies -f body="..."
```

Be specific: "False positive — `cfg` is used on the lines that build `env_prefix`. `make lint` passes cleanly." Don't be defensive; show evidence.

## Mirror rule: relayport ↔ ibkr_bridge

These two projects share `cli/core/deploy.py`, `cli/core/destroy.py`, `cli/core/sync.py`, and adjacent helpers (`ssh_cmd`, `compose_invocation`, `sanity_check.py`). The sibling-project mirror rule (see root [CLAUDE.md](../../../CLAUDE.md)) requires changes there to land in both repos in the same session.

When handling CR for a PR in one repo, **always check whether the same fix needs to land in the sibling repo**. The reviewer for the sibling PR may have flagged the same issue independently, or may not have caught it yet — either way, mirror.

## Why this skill exists

Past CR loops on this repo ran 5+ rounds because:
1. I addressed only the flagged items, never the integrated result.
2. Each new fix added a new branch with its own edge cases.
3. The next round found those edge cases, and I treated each as a fresh surprise.

The adversarial audit step in §4 is the one mechanism that breaks this loop. If you skip it, the loop continues. If you do it honestly — looking specifically for the kinds of issues the *next* reviewer would find — most rounds collapse into 1-2 follow-ups instead of 5.
