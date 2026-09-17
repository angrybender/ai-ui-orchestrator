const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const readline = require('node:readline');
const root = path.resolve(__dirname, '..');
const python = path.join(root, 'dev-venv/bin/python');
const fixture = path.join(__dirname, 'board_live_server.py');

(async () => {
  const server = spawn(python, ['-B', '-u', fixture], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
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
    await page.goto(info.url);
    const button = page.locator('#agent-recovery-button');
    await expect(button).toHaveText('Agent Recovery');
    const newTask = page.locator('#new-task-button');
    const recoveryBox = await button.boundingBox();
    const newBox = await newTask.boundingBox();
    assert(recoveryBox.x + recoveryBox.width <= newBox.x);
    assert.equal(recoveryBox.y, newBox.y);
    await button.click();
    await expect(page.locator('.toast').last()).toHaveText('No agent runs require recovery');
    const created = await page.request.post(info.url + '/api/board/tasks', { data: { task_id: 'REC-1', title: 'Blocked agent', description: 'Recovery check' } });
    assert.equal(created.status(), 201);
    await page.request.patch(info.url + '/api/board/tasks/REC-1/move', { data: { status: 'OPEN', position: 0 } });
    const operation = (name, id) => JSON.parse(execFileSync(python, ['-B', fixture, '--directory', info.directory, '--operation', name, ...(id ? ['--run-id', id] : [])], { cwd: root, encoding: 'utf8' }));
    const run = operation('claim');
    operation('agent', run.id);
    operation('agent-uncertain', run.id);
    page.once('dialog', dialog => dialog.dismiss());
    await button.click();
    await expect(button).toBeEnabled();
    assert.equal((await (await page.request.get(info.url + '/api/agent/recovery')).json()).runs.length, 1);
    let release, arrived, posts = 0;
    const pending = new Promise(resolve => { arrived = resolve; });
    await page.route('**/api/agent/recovery', async route => {
      if (route.request().method() === 'POST') {
        posts++;
        await new Promise(resolve => { release = resolve; arrived(); });
      }
      await route.continue();
    });
    page.once('dialog', dialog => {
      assert(dialog.message().includes('REC-1'));
      return dialog.accept();
    });
    await button.click();
    await pending;
    await expect(button).toBeDisabled();
    await expect(button).toHaveAttribute('aria-busy', 'true');
    await button.dispatchEvent('click');
    assert.equal(posts, 1);
    release();
    await expect(button).toBeEnabled();
    await expect(page.locator('.toast').last()).toHaveText('Agent recovered. Queue unblocked.');
    await page.unroute('**/api/agent/recovery');
    assert.deepEqual((await (await page.request.get(info.url + '/api/agent/recovery')).json()).runs, []);
    const task = await (await page.request.get(info.url + '/api/board/tasks/REC-1')).json();
    assert.equal(task.status, 'WAIT');
    if (process.env.RECOVERY_SCREENSHOTS) await page.screenshot({ path: process.env.RECOVERY_SCREENSHOTS + '-desktop.png' });
    await page.route('**/api/agent/recovery', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Recovery temporarily unavailable' }) }));
    await button.click();
    await expect(page.locator('.toast[role=alert]').last()).toHaveText('Recovery temporarily unavailable');
    await expect(button).toBeEnabled();
    await page.unroute('**/api/agent/recovery');
    await page.setViewportSize({ width: 320, height: 800 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await expect(button).toBeVisible();
    await expect(newTask).toBeVisible();
    if (process.env.RECOVERY_SCREENSHOTS) await page.screenshot({ path: process.env.RECOVERY_SCREENSHOTS + '-mobile.png' });
    await page.goto(info.url + '/tasks/REC-1');
    await expect(page.locator('#agent-recovery-button')).toHaveCount(0);
    assert.deepEqual(errors, []);
    console.log('PASS: recovery placement, empty queue, dismiss/confirm, real recovery API, duplicate guard, Spinner, errors, 320px layout and Board-only visibility');
  } finally {
    if (browser) await browser.close();
    const exited = new Promise(resolve => server.once('exit', resolve));
    server.stdin.end('\n');
    const timer = setTimeout(() => server.kill('SIGKILL'), 10000);
    await exited;
    clearTimeout(timer);
    if (info) assert.equal(fs.existsSync(info.directory), false);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
