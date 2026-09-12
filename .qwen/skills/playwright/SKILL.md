---
name: playwright
description: Browser automation with Playwright — navigate, click, fill forms, screenshot, scrape, and run E2E tests. Optional model argument ("/playwright sonnet run the login tests" or "/playwright --model opus ...") selects the model used for spawned browser-automation agents. Use for any browser automation, web scraping, or E2E testing task.
argument-hint: [task]
allowed-tools:
  - Bash(node *)
  - Bash(npm install *)
  - Bash(npx playwright *)
  - Bash(/root/.npm/_npx/*/node_modules/.bin/playwright *)
  - Bash(export *)
# Model used while this skill is active — same values as /model, or `inherit`.
# Edit this line to pin a default model for every invocation of this skill:
model: inherit
---

# Playwright browser automation

Automate a real browser (Chromium / Firefox / WebKit): navigation, clicking,
form filling, screenshots, scraping, and `playwright/test` suites.

## 1. Model selection

Raw arguments (maybe empty): `$ARGUMENTS`

Extract MODEL from the ENV named `VISION_MODEL`:

- `echo $VISION_MODEL`

**if VISION_MODEL is empty - use default model**

Apply MODEL:

- **Spawned subagents**: pass MODEL as the Agent tool's `model` option for
  every agent doing browser work. MODEL unset → omit the option.
- **Frontmatter**: the `model:` field above pins a per-skill default for the
  whole invocation (static — edit this file to change it). An argument MODEL
  still decides what subagents use.
- **Limits**: a skill cannot switch the current session's own model. If the
  user expects the whole session to switch, tell them to run `/model <name>`.

State the model in use (the chosen MODEL, or "inherited") in your final reply.

## 2. Environment (pre-verified — do not reinstall anything)

Verified working on this machine 2026-08-15. Playwright and all three browsers
are **already installed and functional**. The only requirement is three
environment variables, because the session runs as `root` while `HOME=/home/ubuntu`:

| Variable | Value | Why |
|---|---|---|
| `HOME` | `/root` | Firefox refuses to launch when `$HOME` is owned by another user (ubuntu) |
| `PLAYWRIGHT_BROWSERS_PATH` | `/root/.cache/ms-playwright` | Browsers live in root's cache, not the default `$HOME/.cache` |
| `NODE_PATH` | `/root/.npm/_npx/e41f203b7505f1fb/node_modules` | Lets `require('playwright')` resolve from any directory |

Facts (do not re-check unless something breaks):

- Node v22.23.2, Playwright 1.62.1 (from the npx cache — it is NOT a dependency
  of `/app`; its package.json has no deps)
- Browsers in `/root/.cache/ms-playwright`: chromium-1234, chromium_headless_shell-1234,
  firefox-1538, webkit-2336, ffmpeg-1011 — all marked `INSTALLATION_COMPLETE`
  and `DEPENDENCIES_VALIDATED`. **No `apt-get`, no `sudo`, no
  `npx playwright install` is ever needed** — system libs are already present
  and chromium/firefox/webkit all launch.
- CLI binary: `/root/.npm/_npx/e41f203b7505f1fb/node_modules/.bin/playwright`

Each Bash call is a fresh shell, so the exports and the command must share a
single invocation (paste the whole block, or inline as prefixes):

```bash
export HOME=/root \
  PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
  NODE_PATH=/root/.npm/_npx/e41f203b7505f1fb/node_modules
node script.js            # require('playwright') now works
"$NODE_PATH/.bin/playwright" --version    # → 1.62.1
```

Or as a one-line prefix:

```bash
HOME=/root PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
NODE_PATH=/root/.npm/_npx/e41f203b7505f1fb/node_modules node script.js
```

Set the three variables at the top of every script run and every CLI
invocation — omitting `HOME=/root` breaks Firefox with a misleading
"Executable doesn't exist" / "Failed to launch" error; omitting
`PLAYWRIGHT_BROWSERS_PATH` breaks all browsers the same way.

**Fallback** (only if the npx cache path above ever stops resolving — e.g.
after a Playwright version bump): `npm i playwright` into `/app` and keep
using the two `/root` variables; the browsers in `/root/.cache/ms-playwright`
stay valid.

WSL2 (this machine): browsers run headless by default — keep it that way.
Headed mode / `--headed` needs `$DISPLAY` (WSLg or an X server); verify it is
set before attempting a headed run.

## 3. Do the task

Full snippets live in ${CLAUDE_SKILL_DIR}/references/patterns.md — read it
before writing nontrivial code. Choose a pattern:

- **One-off automation** (scrape, screenshot, form submit) → write a small
  Node script, run it with the env prefix from §2 + `node <script>.js`, print
  results to stdout
- **CLI shortcut** (single screenshot / PDF) →
  `HOME=/root PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
  /root/.npm/_npx/e41f203b7505f1fb/node_modules/.bin/playwright screenshot --full-page <url> out.png`
- **E2E tests** → spec files + `playwright.config.js` (use the `webServer`
  config to auto-start the dev server), run the binary + `test` with the env
  prefix from §2. **Import from `playwright/test`, not `@playwright/test`** —
  only `playwright` is installed (verified: a spec importing `playwright/test`
  passes; `@playwright/test` throws MODULE_NOT_FOUND)
- **Recording selectors** → same binary path + `codegen <url>` (needs `$DISPLAY`)

**For open web-app in the browser**
Project directory mapped to `/var/www/`
For open `index.html` as a file use `browser_run_code` with code:
<code>
"async (page) => { await page.goto('file:///var/www/index.html'); return await page.content(); }"
</code>

Delegate to a subagent (Agent tool, `model` = MODEL) when the work is
parallel or large; otherwise work inline.

## 4. Report

- The commands you ran and their exit codes; the test summary line if tests ran
- Absolute paths of every artifact produced (screenshots, PDFs, traces, videos)
- Which model was used (from step 1)

## Hard rules

- Never hardcode credentials in scripts, specs, or `storageState` files —
  read them from environment variables
- Never auto-click destructive or paid UI actions ("delete", "purchase",
  "send") on a real site without explicit user confirmation first
- Headless by default; always `await browser.close()` in scripts
