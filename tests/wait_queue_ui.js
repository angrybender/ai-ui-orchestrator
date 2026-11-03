const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || 'python3';

(async () => {
  const server = spawn(python, ['-u', path.join(__dirname, 'board_live_server.py'), '--wait-queue'], {
    cwd: root, stdio: ['pipe', 'pipe', 'pipe']
  });
  let serverLog = '';
  server.stderr.on('data', data => { serverLog += data; });
  let browser;
  let info;
  try {
    info = await new Promise((resolve, reject) => {
      const lines = readline.createInterface({ input: server.stdout });
      const timer = setTimeout(() => reject(new Error(`Fixture startup timed out: ${serverLog}`)), 15000);
      lines.once('line', line => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${serverLog}`)); });
    });
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1568, height: 1050 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.goto(info.url, { waitUntil: 'domcontentloaded' });
    const column = status => page.locator(`.board-column[data-status="${status}"]`);
    const card = (id, status) => column(status).locator(`.task-card[data-task-id="${id}"]`);
    for (const [id, mode] of [['WAIT-1', 'failure'], ['WAIT-2', 'hold'], ['WAIT-3', 'success']]) {
      const created = await page.request.post(`${info.url}/api/board/tasks`, {
        data: { task_id: id, title: id, description: mode }
      });
      assert.equal(created.status(), 201);
      const moved = await page.request.patch(`${info.url}/api/board/tasks/${id}/move`, {
        data: { status: 'OPEN', position: 99 }
      });
      assert.equal(moved.status(), 200);
    }
    const launched = await page.request.post(`${info.url}/api/agent/run`);
    assert.equal(launched.status(), 202);
    await expect(card('WAIT-1', 'WAIT')).toBeVisible({ timeout: 15000 });
    await expect(card('WAIT-1', 'WAIT')).toHaveClass(/is-error/);
    await expect(card('WAIT-2', 'IN PROGRESS')).toBeVisible({ timeout: 15000 });
    await expect(card('WAIT-3', 'OPEN')).toBeVisible();
    await expect(column('IN PROGRESS').locator('.column-count')).toHaveText('1');
    fs.writeFileSync(path.join(info.directory, 'release-WAIT-2'), 'release');
    await expect(card('WAIT-2', 'REVIEW')).toBeVisible({ timeout: 15000 });
    await expect(card('WAIT-3', 'OPEN')).toBeVisible();
    await expect(column('IN PROGRESS').locator('.column-count')).toHaveText('0');
    assert.deepEqual(errors, []);
    console.log('PASS: One API run moves the failed task to WAIT and automatically starts the next OPEN task; SSE updates Board.');
  } finally {
    if (info) fs.writeFileSync(path.join(info.directory, 'release-WAIT-2'), 'release');
    if (browser) await browser.close();
    if (server.exitCode === null) {
      server.stdin.end('\n');
      await new Promise(resolve => {
        const timer = setTimeout(() => { server.kill('SIGKILL'); resolve(); }, 5000);
        server.once('exit', () => { clearTimeout(timer); resolve(); });
      });
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
