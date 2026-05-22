# AI Instruction Files — Layout and Sync

This repo gives AI agents (Claude Code, GitHub Copilot) layered instructions so the right rules load at the right time without bloating every prompt.

## Why this exists

- A single 900-line instruction file bloats every Claude/Copilot turn by ~27K tokens. Anthropic's official guidance is **under 200 lines per CLAUDE.md**.
- Claude Code auto-loads ancestor `CLAUDE.md` files **at session start** and descendant `CLAUDE.md` files **on demand** (when Claude reads a file in that subtree).
- GitHub Copilot reads a universal `.github/copilot-instructions.md` plus path-scoped `.github/instructions/*.instructions.md` files (matched by `applyTo:` globs).
- We exploit both lazy-loading mechanisms to keep the always-on context small while still giving full guidance when work touches a specific area.

## File layout

```
CLAUDE.md                          # Root — always-on rules (~180 lines)
docs/
  ARCHITECTURE.md                  # Descriptive prose; not auto-loaded
  INSTRUCTION_FILES.md             # This file
.github/
  copilot-instructions.md          # Mirror of root CLAUDE.md (Copilot universal)
  instructions/                    # Path-scoped Copilot files
    cli.instructions.md            # applyTo: cli/**
    services.instructions.md       # applyTo: services/**
    relay_core.instructions.md     # applyTo: services/relay_core/**
    relays.instructions.md         # applyTo: services/relays/**
    relays-ibkr.instructions.md    # applyTo: services/relays/ibkr/**
    relays-kraken.instructions.md  # applyTo: services/relays/kraken/**
    market_data.instructions.md    # applyTo: services/market_data/**
    debug.instructions.md          # applyTo: services/debug/**
    shared.instructions.md         # applyTo: services/shared/**
    infra.instructions.md          # applyTo: infra/**
    types.instructions.md          # applyTo: types/**
cli/CLAUDE.md                      # Lazy-loaded by Claude
services/CLAUDE.md
services/relay_core/CLAUDE.md
services/relays/CLAUDE.md
services/relays/ibkr/CLAUDE.md
services/relays/kraken/CLAUDE.md
services/market_data/CLAUDE.md
services/debug/CLAUDE.md
services/shared/CLAUDE.md
infra/CLAUDE.md
types/CLAUDE.md
.claude/skills/                    # Long-form playbooks (loaded only when invoked)
  add-relay-adapter/SKILL.md
  refresh-flex-fixtures/SKILL.md
  add-caddy-route/SKILL.md
  export-new-model-to-types/SKILL.md
```

## Maintenance contract

Each `<dir>/CLAUDE.md` has a paired `.github/instructions/<slug>.instructions.md` covering the **same rules** plus a YAML frontmatter:

```markdown
---
applyTo: "<glob matching the same files>"
---

<rules, same content as the CLAUDE.md — see "Allowed divergence" below>
```

**When editing any `CLAUDE.md`, update its mirror in the same commit.** Same for the root `CLAUDE.md` ↔ `.github/copilot-instructions.md` pair. The rule **content** must stay in sync; presentation may differ slightly.

### Allowed divergence

These differences are intentional and don't violate the sync contract:

1. **Cross-references.** `CLAUDE.md` files use Markdown links to other CLAUDE.md files (`[../CLAUDE.md](../CLAUDE.md)`); Copilot mirrors reference siblings by name (`services.instructions.md`) since the directory structure differs.
2. **Glob breadth.** `applyTo:` globs may legitimately cover files outside the CLAUDE.md's directory (e.g. `cli.instructions.md` applies to `Makefile`, `docker-compose*.yml`, and `terraform/**` in addition to `cli/**`, because those files share the same deploy rules). Document such inclusions inline.
3. **Skill pointers.** CLAUDE.md may link to a Skill via Markdown link (`[add-relay-adapter](.claude/skills/add-relay-adapter/SKILL.md)`); the Copilot mirror references it by name only, since Skills are a Claude-specific feature.

### What must stay identical

- Every **rule** (every bullet starting with **"…"**, every numbered procedure step, every code block enforcing a pattern) must appear in both files with the same wording.
- Tables of facts (env vars, error codes, file-to-output mappings) must match row for row.

### Sanity check during PR review

A simple grep is more useful than a strict diff: confirm both files have the same set of rule-bullets:

```bash
diff <(grep -E '^- \*\*' services/X/CLAUDE.md) \
     <(grep -E '^- \*\*' .github/instructions/X.instructions.md)
```

Empty diff = rules match. Anything else = a rule drifted between the two.

## What goes where

- **Root `CLAUDE.md`** — rules that apply to every file in the repo (security, deprecated APIs, type safety, error handling, concurrency, dependencies).
- **Directory `CLAUDE.md`** — rules that only matter when working in that subtree (cli deployment specifics, relay engine internals, broker-specific quirks).
- **`.claude/skills/<name>/SKILL.md`** — multi-step procedures only relevant for rare tasks (adding a new broker, refreshing fixtures, registering a new Caddy route). These cost zero context until invoked.
- **`docs/ARCHITECTURE.md`** — descriptive prose (file trees, system diagrams, design rationale). Not auto-loaded. Claude reads it via Read/Grep when asked architectural questions.

## When to move a rule

| Lives where? | When | Why |
| --- | --- | --- |
| Root | Applies repo-wide or could be violated in any file | Always-on for safety |
| Directory CLAUDE.md | Only applies in that subtree | Lazy-load saves context |
| Skill | Multi-step procedure for a rare task | Zero context cost until invoked |
| ARCHITECTURE.md | Descriptive prose, not enforceable | Reference, not a rule |

If a rule appears in three places, collapse it. Cross-reference instead of duplicating.

## Verifying Claude loads what you expect

When working in `services/relay_core/`, Claude should have:
1. Root `CLAUDE.md` (always)
2. `services/CLAUDE.md` (descendant, loaded on first read)
3. `services/relay_core/CLAUDE.md` (descendant, loaded on first read)

Subdirectory `CLAUDE.md` files **only load when Claude reads a file in that subtree** — purely listing the directory doesn't trigger the load.
