const { chromium } = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(process.env.AGENT_TEST_URL, { waitUntil: 'networkidle' });
    assert.ok((await page.title()).length);
    const result = await page.evaluate(async () => {
      const response = await fetch('/api/agent/run', { method: 'POST' });
      return { status: response.status, body: await response.json() };
    });
    assert.equal(result.status, 202);
    assert.equal(typeof result.body.pid, 'number');
    assert.deepEqual(errors, []);
    console.log('PASS: Board loads without JS errors; agent API returns executor PID.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
