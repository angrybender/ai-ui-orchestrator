const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

const root = path.resolve(__dirname, '..');
const fixture = path.join(__dirname, 'board_live_server.py');
const python = process.env.PYTHON || 'python3';

(async () => {
  const server = spawn(python, ['-u', fixture], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
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
    assert.notEqual(path.resolve(info.directory), path.join(root, 'data'));
    browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1568, height: 1050 } });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await expect.poll(async () => {
      try { return (await context.request.get(`${info.url}/health`)).status(); } catch (_) { return 0; }
    }, { timeout: 15000 }).toBe(200);
    await page.goto(info.url, { waitUntil: 'domcontentloaded' });
    const open = async () => {
      await page.locator('#new-task-button').click();
      await expect(page.locator('#task-modal')).toBeVisible();
    };
    const check = async (title, description) => {
      await expect(page.locator('#task-title')).toHaveValue(title);
      await expect(page.locator('#task-description')).toHaveValue(description);
    };
    await open();
    await page.locator('#task-title').fill('  Черновик задачи  ');
    await page.locator('#task-description').fill('Описание\nВторая строка');
    for (const close of ['#cancel-task', '#close-task']) {
      await page.locator(close).click();
      await open();
      await check('  Черновик задачи  ', 'Описание\nВторая строка');
    }
    await page.locator('#task-description').press('Escape');
    await expect(page.locator('#task-modal')).toBeHidden();
    await open();
    await check('  Черновик задачи  ', 'Описание\nВторая строка');
    await page.locator('#task-modal').click({ position: { x: 2, y: 2 } });
    await open();
    await check('  Черновик задачи  ', 'Описание\nВторая строка');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await open();
    await check('  Черновик задачи  ', 'Описание\nВторая строка');
    await page.route('**/api/board/tasks', route => route.request().method() === 'POST'
      ? route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Test conflict' }) })
      : route.continue());
    await page.locator('#save-task').click();
    await expect(page.locator('.toast[role="alert"]')).toContainText('Test conflict');
    await page.locator('#cancel-task').click();
    await open();
    await check('  Черновик задачи  ', 'Описание\nВторая строка');
    await page.unroute('**/api/board/tasks');
    const savedId = await page.locator('#task-id').inputValue();
    await page.locator('#save-task').click();
    await expect(page.locator('#task-modal')).toBeHidden();
    assert.equal(await page.evaluate(() => localStorage.getItem('orchestrator.newTaskDraft')), null);
    await open();
    await check('', '');
    await page.locator('#task-title').fill('Другой черновик');
    await page.locator('#task-description').fill('Не менять при редактировании');
    await page.locator('#cancel-task').click();
    await page.locator(`.task-card[data-task-id="${savedId}"]`).click();
    await page.locator('#task-title').fill('Несохранённое изменение');
    await page.locator('#task-title').press('Escape');
    await expect(page.locator('#task-modal')).toBeHidden();
    await page.locator(`.task-card[data-task-id="${savedId}"]`).click();
    await check('Черновик задачи', 'Описание\nВторая строка');
    await page.locator('#task-title').fill('Изменённая задача');
    await page.locator('#save-task').click();
    await expect(page.locator('#task-modal')).toBeHidden();
    await open();
    await check('Другой черновик', 'Не менять при редактировании');
    if (process.env.TASK_DRAFT_SCREENSHOTS === '1') {
      await page.screenshot({ path: path.join(root, 'task-draft-desktop.png') });
    }
    await page.setViewportSize({ width: 375, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.TASK_DRAFT_SCREENSHOTS === '1') {
      await page.screenshot({ path: path.join(root, 'task-draft-mobile.png') });
    }
    const standalone = await context.newPage();
    await standalone.goto(`${info.url}/tasks/${savedId}`, { waitUntil: 'domcontentloaded' });
    await expect(standalone.locator('#task-title')).toHaveValue('Изменённая задача');
    await standalone.locator('#task-title').press('Escape');
    await expect(standalone.locator('#task-modal')).toBeVisible();
    await standalone.close();
    // Broken or inaccessible browser storage must not prevent task creation.
    for (const unavailable of [false, true]) {
      await page.addInitScript(block => {
        if (block) {
          for (const method of ['getItem', 'setItem', 'removeItem']) {
            Storage.prototype[method] = () => { throw new Error('Storage blocked'); };
          }
        } else localStorage.setItem('orchestrator.newTaskDraft', '{invalid');
      }, unavailable);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await open();
      await check('', '');
      await page.locator('#task-title').fill('Memory fallback');
      await page.locator('#task-description').fill('Fallback description');
      await page.locator('#cancel-task').click();
      await open();
      await check('Memory fallback', 'Fallback description');
      await page.locator('#save-task').click();
      await expect(page.locator('#task-modal')).toBeHidden();
      await open();
      await check('', '');
    }
    assert.deepEqual(errors, []);
    console.log('PASS: draft close/reopen, backdrop, reload, failed/successful creation, edit isolation, mobile, corrupt/unavailable storage.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && server.signalCode === null && server.pid) {
      const exited = new Promise(resolve => server.once('exit', resolve));
      server.stdin.end('\n');
      const timeout = setTimeout(() => server.kill('SIGKILL'), 10000);
      await exited;
      clearTimeout(timeout);
    }
    if (info) assert.equal(fs.existsSync(info.directory), false, 'Fixture must clean its temporary database');
    if (serverLog.includes('Traceback')) console.error(serverLog);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
