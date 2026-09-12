# Playwright Skill — Usage Guide

A Claude Code skill for browser automation: navigation, clicking, form
filling, screenshots, scraping, and end-to-end tests with Playwright.

- **Files:**
  - `SKILL.md` — the skill (invocation logic + instructions)
  - `references/patterns.md` — detailed code patterns the skill reads on demand
  - `readme-skill-playwright.md` — this guide

Claude can also trigger the skill automatically when your request matches its
description (e.g. "scrape this page", "run the E2E tests", "take a screenshot
of the site").

---

## Quick start

1. **Restart Claude Code** (or open a new session) so the skill is picked up.
2. Invoke it as a slash command:

   ```
   /playwright <task>
   ```

Examples:

| You type | What happens |
|---|---|
| `/playwright screenshot https://example.com full page` | Saves a full-page PNG, reports its path |
| `/playwright scrape https://example.com all product names and prices` | Writes a small Node script, prints extracted data |
| `/playwright run the login tests` | Runs `npx playwright test` if a suite exists, or scaffolds one |
| `/playwright record selectors for http://localhost:3000` | Runs `npx playwright codegen` |

No arguments → the skill asks you what task to perform.

---

## Setting the model

The distinctive feature: you choose which model handles the browser work.

### Way 1 — per invocation (dynamic)

Put a model name as the **first token**, or use `--model`:

```
/playwright sonnet screenshot https://example.com
/playwright --model opus run the login tests
/playwright --model=haiku scrape the docs page
/playwright claude-opus-5 fill the signup form with test data
```

**Valid values:** `haiku`, `sonnet`, `opus`, `fable`, `inherit`, or a full
model id (e.g. `claude-opus-5`). Case-insensitive.

- The model applies to every **subagent spawned** for the browser work.
- An unrecognized name → the skill asks you to clarify rather than guessing.
- The chosen model is stated in the final reply.

### Way 2 — pinned default (static)

Edit the `model:` line in `~/.claude/skills/playwright/SKILL.md`:

```yaml
model: inherit   # change to: sonnet, opus, haiku, or a full model id
```

This sets the model for **every** invocation of the skill. An explicit model
argument (Way 1) still wins for subagents.

### Limitation

A skill can change the model for itself and its subagents, **not the whole
session**. To switch the session model, run `/model <name>` yourself.

---

## What the skill does on each run

1. **Parses the model argument** (see above).
2. **Sets the environment** — Playwright 1.62.1 and all three browsers are
   pre-installed on this machine (in the npx cache and `/root/.cache/ms-playwright`).
   Every command runs with `HOME=/root`, `PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright`
   and `NODE_PATH=/root/.npm/_npx/e41f203b7505f1fb/node_modules`, because the
   session user is `root` while `$HOME` points to `/home/ubuntu`. No installs,
   no browser downloads, no sudo — ever.
3. **Picks a pattern** for the task:
   - One-off script (`node script.js` with the env prefix) for scraping / screenshots / form fills
   - `playwright screenshot` / `playwright pdf` (CLI binary, env-prefixed) for single-shot captures
   - Test suite via the same `playwright test` binary — specs import from
     `playwright/test` (not `@playwright/test`, which is not installed)
   - `playwright codegen` for recording selectors (needs `$DISPLAY`)
4. **Reports**: commands run with exit codes, test summary line, absolute paths of all artifacts (screenshots, PDFs, traces, videos), and the model used.

---

## Permissions

The skill pre-approves these for its invocation turn (fewer prompts):

```
Bash(node *)  Bash(npm install *)  Bash(npx playwright *)
Bash(/root/.npm/_npx/*/node_modules/.bin/playwright *)  Bash(export *)
```

Anything else (`sudo`, other commands) still goes through the normal
permission flow — though nothing on this machine needs sudo anymore.

## Safety rules built into the skill

- **Headless by default** (required on this WSL2 machine for headed mode to
  work, `$DISPLAY` must be set).
- **No hardcoded credentials** — secrets are read from environment variables.
- **No destructive/paid auto-clicks** — "delete", "purchase", "send" actions
  on real sites require your explicit confirmation first.
- Scripts always close the browser (`await browser.close()`).

---

## Common tasks

All commands need the env prefix (`HOME=/root PLAYWRIGHT_BROWSERS_PATH=...`)
and the CLI binary from the npx cache — see SKILL.md §2 for both.

```bash
# Watch tests run (needs $DISPLAY — WSLg or an X server)
playwright test --headed

# HTML report of the last run
playwright show-report

# Inspect a failure trace
playwright show-trace trace.zip

# Update snapshot baselines
playwright test -u
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/playwright` doesn't appear | Restart the session; check the file is at `.claude/skills/playwright/SKILL.md` in the project |
| "Executable doesn't exist" / "Failed to launch the browser process" | The env prefix is missing — set `HOME=/root` and `PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright` (SKILL.md §2) |
| Firefox: "$HOME is /home/ubuntu which is owned by ubuntu" | Same fix — `HOME=/root` must be set on the command |
| `Cannot find module 'playwright'` | `NODE_PATH=/root/.npm/_npx/e41f203b7505f1fb/node_modules` missing, or the cache path changed — see the fallback in SKILL.md §2 |
| `Cannot find module '@playwright/test'` | Expected — import from `playwright/test` instead (verified working) |
| Headed mode fails / blank | Headed needs `$DISPLAY` (WSLg/X server) — stay headless |
| Model didn't change | Skills can't switch the session model — use `/model <name>`, or pass the model as the skill argument (it applies to subagents) |
| Model name rejected | Must be `haiku`/`sonnet`/`opus`/`fable`/`inherit` or a full model id |
