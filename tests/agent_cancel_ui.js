const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const readline = require('node:readline');
const root = path.resolve(__dirname, '..');

async function check(mode, standalone) {
  const server = spawn(path.join(root, 'dev-venv/bin/python'), ['-B', '-u', path.join(__dirname, 'agent_cancel_server.py'), '--mode', mode],
    { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
  let browser, info, log = '';
  server.stderr.on('data', data => { log += data; });
  try {
    info = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(log || 'Fixture timeout')), 15000);
      readline.createInterface({ input: server.stdout }).once('line', line => { clearTimeout(timer); resolve(JSON.parse(line)); });
      server.once('error', reject);
    });
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1459, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    const state = async () => (await page.request.get(info.url + '/fixture-state')).json();
    await expect.poll(async () => {
      try { return (await page.request.get(info.url + '/api/board/tasks')).ok(); } catch { return false; }
    }, { timeout: 15000 }).toBe(true);
    await page.goto(info.url + (standalone ? '/tasks/CANCEL-1' : '/'));
    if (!standalone) await page.locator('.task-card[data-task-id="CANCEL-1"]').click();
    await expect(page.locator('#task-status')).toHaveValue('IN PROGRESS');
    await expect(page.locator('#stop-task')).toBeVisible();
    if (process.env.CANCEL_SCREENSHOTS) await page.screenshot({ path: process.env.CANCEL_SCREENSHOTS + '-' + mode + '-running.png', fullPage: true });
    await page.locator('#task-title').fill('Updated before stopping');
    const saved = page.waitForResponse(response => response.request().method() === 'PUT' && response.url().endsWith('/api/board/tasks/CANCEL-1'));
    await page.locator('#stop-task').click();
    assert.equal((await saved).status(), 200);
    if (standalone) await expect(page.locator('#task-status')).toHaveValue('BACKLOG');
    else await expect(page.locator('.board-column[data-status="BACKLOG"] .task-card')).toBeVisible();
    await expect.poll(async () => (await state()).cancel_received).toBe(true);
    if (mode === 'ignore') {
      const pending = await state();
      assert.equal(pending.state, 'RUNNING');
      assert.equal(pending.ssh_closed, false);
      assert.equal(pending.agent_alive, true);
      console.log('Checking the full 60-second ACP cancellation deadline...');
    }
    await expect.poll(async () => (await state()).worker_alive, { timeout: 70000, intervals: [100, 500, 1000] }).toBe(false);
    const stopped = await state();
    assert.equal(stopped.status, 'BACKLOG');
    assert.equal(stopped.state, 'FAILED');
    assert.equal(stopped.stop_reason, 'cancelled');
    assert.equal(stopped.confirmed, true);
    assert.equal(stopped.uncertain, false);
    assert.equal(stopped.ssh_closed, true);
    assert.equal(stopped.agent_alive, false);
    assert.equal(stopped.child_alive, false);
    if (mode === 'ignore') assert(stopped.elapsed_since_cancel >= 59.5 && stopped.elapsed_since_cancel < 65);
    await expect(page.locator('#task-title')).toHaveValue('Updated before stopping');
    await expect(page.locator('#stop-task')).toBeHidden();
    await expect(page.locator('#chat-composer')).toBeVisible();
    await expect(page.locator('.chat-message-body')).toContainText('Remote agent cancelled.');
    if (process.env.CANCEL_SCREENSHOTS) await page.screenshot({ path: process.env.CANCEL_SCREENSHOTS + '-' + mode + '.png', fullPage: true });
    await page.reload();
    if (!standalone) await page.locator('.task-card[data-task-id="CANCEL-1"]').click();
    await expect(page.locator('#task-status')).toHaveValue('BACKLOG');
    await expect(page.locator('.chat-message-body')).toContainText('Remote agent cancelled.');
    await page.locator('#chat-comment').fill('Continue with the updated requirements');
    await page.locator('#send-comment').click();
    await expect(page.locator('.chat-user')).toContainText('Continue with the updated requirements');
    await expect(page.locator('#task-status')).toHaveValue('BACKLOG');
    assert.equal((await state()).status, 'BACKLOG');
    await page.reload();
    if (!standalone) await page.locator('.task-card[data-task-id="CANCEL-1"]').click();
    await expect(page.locator('.chat-user')).toContainText('Continue with the updated requirements');
    await page.setViewportSize({ width: 375, height: 950 });
    assert(await page.evaluate(() => document.querySelector('.task-dialog').scrollWidth <= document.querySelector('.task-dialog').clientWidth));
    if (process.env.CANCEL_SCREENSHOTS) await page.screenshot({ path: process.env.CANCEL_SCREENSHOTS + '-' + mode + '-mobile.png', fullPage: true });
    await page.locator('#task-status').selectOption('OPEN');
    await page.locator('#save-task').click();
    await expect.poll(async () => (await state()).status).toBe('OPEN');
    assert.deepEqual(errors, []);
    console.log('PASS:', standalone ? 'standalone form' : 'modal form', mode, JSON.stringify(stopped));
  } finally {
    if (browser) await browser.close();
    if (log) console.error(log);
    const exited = server.exitCode !== null || server.signalCode !== null ? Promise.resolve() : new Promise(resolve => server.once('exit', resolve));
    server.stdin.end('\n');
    const timer = setTimeout(() => server.kill('SIGKILL'), 10000);
    await exited;
    clearTimeout(timer);
    if (info) assert.equal(fs.existsSync(info.directory), false);
  }
}

(async () => {
  await check('cooperative', false);
  await check('ignore', true);
})().catch(error => { console.error(error); process.exitCode = 1; });
