# Playwright patterns

Detailed snippets for common tasks. Everything is already installed — see the
env-prefix block below; do NOT run `npm install`, `npx playwright install`, or
`install-deps` (verified unnecessary on this machine, system libs included).

## Environment (required for every run)

The session runs as `root` with `HOME=/home/ubuntu`, so each Bash call needs
these three variables (inline them, or export in the same command — each Bash
call is a fresh shell):

```bash
export HOME=/root \
  PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
  NODE_PATH=/root/.npm/_npx/e41f203b7505f1fb/node_modules
PW_BIN="$NODE_PATH/.bin/playwright"     # CLI: v1.62.1
```

- Node v22.23.2; browsers chromium-1234 / firefox-1538 / webkit-2336 all launch.
- Use `chromium` unless the task names another browser (`firefox`, `webkit`).
- Specs import from **`playwright/test`** — `@playwright/test` is not
  installed and will throw MODULE_NOT_FOUND.
- WSL2 / headless server: stay headless (the default). Headed mode needs WSLg
  or an X server (`$DISPLAY` set); check before trying `headless: false`.

## One-off automation script

```js
// scrape.js — run: HOME=/root PLAYWRIGHT_BROWSERS_PATH=... NODE_PATH=... node scrape.js
// (env prefix from the top of this file)
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();          // headless by default
  const page = await browser.newPage();

  await page.goto('https://example.com', { waitUntil: 'domcontentloaded' });

  console.log('title:', await page.title());
  await page.screenshot({ path: 'shot.png', fullPage: true });

  const links = await page.$$eval('a', as => as.map(a => a.href));
  console.log(links.slice(0, 10));

  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
```

### Interacting

```js
await page.click('text=Login');                          // accessible-name
await page.fill('#email', 'user@example.com');           // no waiting needed
await page.press('#search', 'Enter');
await page.selectOption('select#country', 'FR');
await page.check('input[name=accept]');
await page.waitForSelector('.results');                  // explicit waits
await page.waitForLoadState('networkidle');              // SPA settle — use sparingly
```

Selector preference order: accessible role/text (`getByRole`, `getByText`,
`text=`) → `data-testid` → CSS. Avoid brittle XPath.

### Scraping SPA content

```js
await page.goto(url, { waitUntil: 'networkidle' });
const items = await page.$$eval('.item', nodes =>
  nodes.map(n => ({ title: n.querySelector('h3')?.innerText, price: n.querySelector('.price')?.innerText }))
);
```

Intercept network if data comes from JSON APIs — often cleaner than DOM parsing:

```js
page.on('response', r => { if (r.url().includes('/api/items')) console.log(r.json()); });
```

### Auth (storageState) — login once, reuse

```js
// auth.js
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('https://app.example.com/login');
  await page.fill('#email', process.env.USER_EMAIL);
  await page.fill('#password', process.env.USER_PASS);
  await page.click('button[type=submit]');
  await page.waitForURL('**/dashboard');
  await page.context().storageState({ path: 'state.json' });
  await browser.close();
})();
```

Then `newContext({ storageState: 'state.json' })` (or set `use: { storageState }`
in playwright.config).

## Test suite

`playwright.config.js` (import from `playwright/test`):

```js
const { defineConfig } = require('playwright/test');

export default defineConfig({
  testDir: './tests',
  timeout: 30_000,
  use: { baseURL: 'http://localhost:3000', trace: 'on-first-retry' },
  webServer: {                                   // auto-starts the dev server
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: true,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
});
```

`tests/smoke.spec.js`:

```js
const { test, expect } = require('playwright/test');

test('login flow', async ({ page }) => {
  await page.goto('/login');
  await page.fill('#email', 'user@example.com');
  await page.fill('#password', 'secret');
  await page.click('button[type=submit]');
  await expect(page).toHaveURL(/dashboard/);
  await expect(page.getByRole('heading', { name: 'Welcome' })).toBeVisible();
});
```

Run / debug (env prefix + `$PW_BIN` as defined at the top of this file):

```bash
"$PW_BIN" test                                    # all
"$PW_BIN" test tests/smoke.spec.js -g "login"     # filtered
"$PW_BIN" test --headed                           # watch it (needs $DISPLAY)
"$PW_BIN" test --trace on                         # full traces
"$PW_BIN" test -u                                 # update snapshots
"$PW_BIN" show-report                             # HTML report of last run
"$PW_BIN" codegen http://localhost:3000           # record actions → code (needs $DISPLAY)
"$PW_BIN" show-trace trace.zip
```

## CLI screenshots / PDF (no code needed)

```bash
HOME=/root PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
  "$PW_BIN" screenshot --full-page https://example.com out.png
HOME=/root PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
  "$PW_BIN" pdf https://example.com out.pdf       # chromium only
```
