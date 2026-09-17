# Playwright patterns for Codex

Use the package and browser paths verified during the skill's environment check.
Examples use CommonJS and Node's built-in assertions. Run from the project root;
use `.cjs` for temporary scripts so package module settings do not change semantics.

## One-off navigation and visual capture

Set `PW_TARGET_URL` to an authorized URL, or `PW_HTML_PATH` to a real standalone
HTML file. Set `PW_SCREENSHOT` to a temporary screenshot path when visual review
is needed. These variables belong to the same shell invocation as the script.

```js
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

(async () => {
  const target = process.env.PW_TARGET_URL ||
    pathToFileURL(path.resolve(process.env.PW_HTML_PATH || 'index.html')).href;
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error') errors.push(message.text());
    });
    await page.goto(target, { waitUntil: 'domcontentloaded' });
    await page.locator('body').waitFor({ state: 'visible' });
    // Add assertions for the requested behavior here; body visibility alone
    // is only a navigation smoke check, not application verification.
    if (process.env.PW_SCREENSHOT) {
      await page.screenshot({ path: process.env.PW_SCREENSHOT, fullPage: true });
    }
    assert.deepEqual(errors, [], 'Browser errors');
    console.log(JSON.stringify({ title: await page.title(), url: page.url() }));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
```

Open the screenshot and specification images with the harness image tool before
removing temporary captures. Use the viewport from the relevant reference instead
of the example dimensions. If a browser error is expected, explain it and assert
the specific expected case rather than suppressing all errors.

## Locators, API waits, and scraping

Prefer `getByRole`, `getByLabel`, or stable test IDs over positional CSS selectors.
Adapt names to the real UI. Scope ambiguous locators to their container.

```js
await page.getByLabel('Email').fill(process.env.TEST_USER_EMAIL);
await page.getByLabel('Password').fill(process.env.TEST_USER_PASSWORD);
await page.getByRole('button', { name: 'Sign in', exact: true }).click();
await page.waitForURL('**/dashboard');

// Register the wait before the action that triggers the response.
const responsePromise = page.waitForResponse(response =>
  new URL(response.url()).pathname === '/api/items' && response.ok()
);
await page.getByRole('button', { name: 'Refresh', exact: true }).click();
const items = await (await responsePromise).json();

// Wait for a meaningful loaded state before extracting DOM content.
await page.getByTestId('results-ready').waitFor();
const rows = await page.locator('.item').evaluateAll(nodes => nodes.map(node => ({
  title: node.querySelector('h3')?.textContent?.trim(),
  price: node.querySelector('.price')?.textContent?.trim(),
})));
```

For login reuse, save `await context.storageState({ path: temporaryStatePath })`
after successful authentication and pass that path to `browser.newContext`.
Treat the file as a secret, keep it out of version control, and delete it after
testing. Do not output API responses containing credentials or personal data.

## Existing test suites

Prefer the project's runner and configuration. Use `require('@playwright/test')`
when that dependency is installed; use `require('playwright/test')` when only the
full `playwright` package provides the runner. Keep the config and specs on the
same package/version. Never mix `require` with `export default`.

For an isolated temporary suite with `playwright` installed:

```js
// playwright.config.cjs
const { defineConfig } = require('playwright/test');
module.exports = defineConfig({
  testDir: './tests',
  timeout: 30_000,
  reporter: 'list',
  outputDir: './test-results',
  use: { headless: true, trace: 'off' },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
```

```js
// tests/smoke.spec.cjs
const { test, expect } = require('playwright/test');
test('the target document loads', async ({ page }) => {
  if (!process.env.PW_TARGET_URL) throw new Error('PW_TARGET_URL is required');
  await page.goto(process.env.PW_TARGET_URL, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('body')).toBeVisible();
  // Extend with task-specific interactions and observable expectations.
});
```

`PW_TARGET_URL` may be a real `file://` URL or a local test server URL. Add
`webServer` only for a backend-dependent page, using the verified project command
and isolated test configuration; do not invent `npm run dev`. Confirm a reused
server is a test instance before sending mutations.

Set `PW_CLI` to the verified absolute `playwright/cli.js` path, obtained with
`require('node:path').join(require('node:path').dirname(require.resolve('playwright/package.json')), 'cli.js')`.
Check that the file exists; `playwright/cli` is not necessarily an exported package
subpath. With a temporary installation,
scope `NODE_PATH` to its `node_modules` directory in the same command.

```bash
node "$PW_CLI" test --config /absolute/path/to/playwright.config.cjs
node "$PW_CLI" screenshot --full-page "$PW_TARGET_URL" "$PW_SCREENSHOT"
node "$PW_CLI" pdf "$PW_TARGET_URL" "$PW_PDF"
```

PDF output requires Chromium. Codegen and headed tests need a usable display.
Trace collection, video, and HTML reports are optional diagnostics; inspect and
then remove those temporary outputs. Keep intentional permanent regression tests.
