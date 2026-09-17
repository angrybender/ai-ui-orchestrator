---
name: playwright
description: Automate a real browser with Playwright for navigation, forms, screenshots, scraping, visual UI verification, and end-to-end tests. Use when a task requires browser interaction or checking a web app against its specifications.
---

# Playwright for Codex

Use this project skill with `$playwright <task>` or select it when browser work
is required. Resolve supporting paths relative to this file.

## Codex harness

- Use the shell execution tool exposed by the current harness (for example,
  `exec_command`, directly or through `functions.exec` and `tools.exec_command`).
  Inspect tool schemas; do not assume Claude's `Bash` or `Agent` tools exist.
- If a Playwright MCP tool is actually available, it can perform interactive
  browser work. Use its exposed schema and filesystem mapping. Otherwise use
  local Node.js scripts. Do not assume a `browser_run_code` tool or `/var/www`
  mount exists.
- Inspect screenshots and reference images with the available image-viewing
  tool, such as `view_image`. A saved screenshot alone is not visual validation.
- Use the current session model. This skill does not switch models or interpret
  `VISION_MODEL`, model arguments, or a `model` frontmatter field. It does not
  request subagents or grant tool permissions; follow the harness instructions.

## Workflow

1. Read the applicable `AGENTS.md` and task-relevant `specs/` files, including
   referenced images, before changing the application. State a short plan and
   choose routine implementation details without asking the user.
2. Inspect existing browser tests, package configuration, and available tools.
   Verify Node.js, package resolution, and a browser launch before relying on
   an installation. Start with `node --version` and
   `node -e "console.log(require.resolve('playwright'))"` from the project root.
   For test suites, resolve `@playwright/test` or `playwright/test` separately;
   use the entry point that the project's installed runner supports.
3. Reuse an installed package if possible. If it resides outside the project,
   use its verified absolute path or a command-scoped `NODE_PATH`. Discover
   npm-cache paths rather than copying a hash from another machine. Never
   overwrite `HOME` or assume a browser cache location/version. Set
   `PLAYWRIGHT_BROWSERS_PATH` only to a verified matching browser installation.
   Environment variables do not persist across independent shell calls.
4. If dependencies are missing, follow the project's package workflow. For
   one-off automation without a project Node dependency, install Playwright
   into a dedicated temporary directory with `npm install --prefix` and invoke
   that installation's CLI to install the required browser. Install system
   browser dependencies only when a launch error demonstrates the need and
   execution permissions allow it. Do not silently modify application manifests.
5. Follow the development-environment setup in `AGENTS.md` before project tests:
   recreate/update `dev-venv` and install `requirements.txt`; never use production
   `venv`. Install the matching system venv package if `ensurepip` is missing.
   Browser tests supplement the relevant application tests.
6. Choose a focused one-off script, an existing test suite, or a CLI operation.
   Read [references/patterns.md](references/patterns.md) for code examples and
   runner setup. Use CommonJS (`require`, `module.exports`) in Node examples;
   keep browser application JavaScript free of imports and build requirements.
7. For standalone HTML, navigate to the actual absolute `file://` URL (construct
   it with `pathToFileURL`). For backend-rendered pages, use the project's
   documented local server and isolated test data. Do not introduce a server
   solely to hide broken standalone-file behavior. Do not point mutation tests
   at production storage or existing agent runs.
8. Verify the requested behavior with assertions, collect relevant console and
   page errors, and inspect screenshots against specification images for UI
   changes. Match the reference viewport; check relevant states and responsive
   sizes. Prefer locator/assertion waits over fixed sleeps or `networkidle`,
   particularly on pages with SSE or polling. Do not update image baselines just
   to accept an unexplained mismatch.
9. Update affected specifications after fixes. Close browsers in `finally`, stop
   only servers started for this task, and delete task-created temporary scripts,
   screenshots, traces, reports, authentication states, and other temporary files.
   In `data/logs`, remove only identified test logs created by this run. Preserve
   user-requested deliverables; they are not temporary verification artifacts.

## Browser defaults and authorization

Use Chromium headless unless the task requires another engine. Verify a display
is available before headed mode or codegen. Read credentials from the environment;
never print or commit secrets, cookies, or storage state. Execute real-site sends,
purchases, or destructive actions only within explicit user authorization already
given; do not ask again for authorized work. If required authorization is absent,
complete the safe preparation and report the blocked action.

## Report

Summarize the result, specifications read/updated, checks actually run and their
outcomes, and any limitations. Distinguish functional checks from visual review.
Link retained deliverables with absolute paths; do not link deleted temporary
screenshots or claim a browser check passed without executing it.
