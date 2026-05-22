---
name: refresh-flex-fixtures
description: Refresh the IBKR Flex XML test fixtures (activity_flex_sample.xml or trade_confirm_sample.xml) from a live response, with sanitization. Use when the user asks to "refresh fixtures", "update IBKR fixtures", "sanitize a Flex dump", or works with services/relays/ibkr/fixtures/.
---

# Refreshing IBKR Flex fixtures

Live IBKR Flex XML responses contain real account IDs, execution IDs, and order IDs. **They must never be committed.** This skill walks through the sanitize-and-commit workflow.

## One-shot refresh (recommended)

```bash
make ibkr-flex-refresh           # primary account
make ibkr-flex-refresh S=_2      # second account (uses IBKR_FLEX_QUERY_ID_2)
```

This runs `flex_dump.py` to fetch a live response into `fixtures/raw.xml` (gitignored), auto-detects the report type (Activity Flex vs Trade Confirmation), and runs `fixtures/sanitize.py` to write the appropriate committed fixture file.

## Two-step (debugging)

```bash
make ibkr-flex-dump              # writes fixtures/raw.xml (gitignored)
python -m relays.ibkr.fixtures.sanitize fixtures/raw.xml --out fixtures/<target>.xml
```

## What `sanitize.py` does

1. Replaces real account IDs (`U1234567`) with synthetic ones (`UXXXXXXX`).
2. Replaces real order IDs and execution IDs with deterministic synthetic values.
3. Trims the fixture to at most 6 distinct orders (`_MAX_ORDERS = 6`), keeping all executions for the retained orders. This keeps fixtures compact while preserving coverage.

## What the committed fixtures look like

Two files, both committed:
- `services/relays/ibkr/fixtures/activity_flex_sample.xml` — for Activity Flex queries (FlexQueryResponse / FlexStatements / FlexStatement / Trades).
- `services/relays/ibkr/fixtures/trade_confirm_sample.xml` — for Trade Confirmation queries.

## Things to verify before committing

- `grep -E '(U[0-9]{7,}|[0-9]{15,})' fixtures/<target>.xml` should return only synthetic IDs (`UXXXXXXX`, padded synthetic numbers).
- The fixture should still parse: run `make test` — IBKR parser tests load these fixtures and exercise the full parsing path.
- The fixture size: rough sanity check `wc -l` (Activity ~few hundred lines, Trade Confirmation similar).
- `git status` should show only the committed fixture changed, not `fixtures/raw.xml`.

## When to add a new fixture

Only when adding coverage for a new IBKR report type or a new edge case the existing fixtures don't cover. Most changes should refresh existing fixtures, not add new files.

## Why this exists

Past incident: an unsanitized raw Flex dump containing real account IDs and execution IDs was committed to a public repo. The sanitize-then-commit workflow is mandatory.
