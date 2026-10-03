const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');

(async () => {
  const server = spawn(process.env.PYTHON || 'python3', ['-u', path.join(__dirname, 'board_live_server.py')], { stdio: ['pipe', 'pipe', 'inherit'] });
  let browser;
  try {
    const info = await new Promise((resolve, reject) => {
      readline.createInterface({ input: server.stdout }).once('line', line => resolve(JSON.parse(line)));
      server.once('error', reject);
      server.once('exit', code => reject(new Error(`Fixture exited: ${code}`)));
    });
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 721, height: 1000 } });
    const errors = [];
    let saves = 0;
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/board/tasks')) saves++; });
    await page.goto(info.url);
    await page.locator('#new-task-button').click();
    await expect(page.locator('#task-id')).not.toHaveValue('');
    await page.locator('#task-title').fill('Validation');
    await page.locator('#task-description').fill('Details');
    for (const id of ['A/B', '../task', 'A\\B', 'A.B', 'A:B', 'A B', 'A*B', 'A?B', 'A|B']) {
      await page.locator('#task-id').fill(id);
      await page.locator('#save-task').click();
      await expect(page.locator('[data-error="task_id"]')).toContainText('only letters');
      await expect(page.locator('#save-task')).toBeEnabled();
      await expect(page.locator('#task-modal')).toBeVisible();
      assert.equal(await page.locator('#task-title').inputValue(), 'Validation');
      if (id === 'A/B' && process.env.PW_SCREENSHOT) await page.screenshot({ path: process.env.PW_SCREENSHOT });
    }
    assert.equal(saves, 0);
    await page.setViewportSize({ width: 375, height: 800 });
    assert.equal(await page.locator('.task-dialog').evaluate(node => node.scrollWidth <= node.clientWidth), true);
    await page.locator('#task-id').fill('Safe_ID-123');
    await page.locator('#save-task').click();
    await expect(page.locator('#task-modal')).toBeHidden();
    await expect(page.locator('.task-card')).toContainText('Safe_ID-123');
    assert.equal(saves, 1);
    assert.deepEqual(errors, []);
    console.log('PASS: invalid IDs blocked before request, inline errors, preserved values, safe ID saved and mobile layout');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once('exit', resolve); });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
