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
    let navigations = 0;
    page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
    const stream = page.waitForResponse(response => response.url().endsWith('/api/board/events'));
    await page.goto(info.url, { waitUntil: 'domcontentloaded' });
    const streamResponse = await stream;
    assert.equal(streamResponse.status(), 200);
    assert.match(streamResponse.headers()['content-type'], /text\/event-stream/);

    // All writes use the real HTTP API or a distinct Python agent process, never route mocks.
    async function api(method, suffix, body) {
      return page.evaluate(async ({ method, suffix, body }) => {
        const response = await fetch(`/api/board/tasks${suffix}`, {
          method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body)
        });
        return { status: response.status, body: await response.json() };
      }, { method, suffix, body });
    }
    function agent(operation, runId) {
      const args = [fixture, '--directory', info.directory, '--operation', operation];
      if (runId) args.push('--run-id', runId);
      return JSON.parse(execFileSync(python, args, { cwd: root, encoding: 'utf8', timeout: 10000 }));
    }
    const card = id => page.locator(`.task-card[data-task-id="${id}"]`);
    const column = status => page.locator(`.board-column[data-status="${status}"]`);
    async function state(id, status, count = 1) {
      await expect(column(status).locator(`.task-card[data-task-id="${id}"]`)).toBeVisible({ timeout: 15000 });
      await expect(column(status).locator('.column-count')).toHaveText(String(count));
    }
    async function screenshot(name) {
      // Opt-in artifacts for manual comparison; caller removes them after inspection.
      if (process.env.BOARD_LIVE_SCREENSHOTS === '1') {
        await page.screenshot({ path: path.join(root, `board-live-${name}.png`), fullPage: true });
      }
    }
    const id = 'LIVE-1';
    assert.equal((await api('POST', '', { task_id: id, title: 'Cross-process agent execution', description: 'Original description' })).status, 201);
    await state(id, 'BACKLOG');
    for (let edit = 1; edit <= 3; edit++) {
      await card(id).click();
      await page.locator('#task-title').fill(`Backlog edit ${edit}`);
      await page.locator('#task-description').fill(`Description after edit ${edit}`);
      await page.getByRole('button', { name: 'Save', exact: true }).click();
      await expect(page.locator('#task-modal')).toBeHidden();
      await state(id, 'BACKLOG');
      await expect(card(id)).toContainText(`Backlog edit ${edit}`);
    }
    assert.equal((await api('PATCH', `/${id}/move`, { status: 'OPEN', position: 0 })).status, 200);
    await state(id, 'OPEN');
    await expect(column('BACKLOG').locator('.column-count')).toHaveText('0');
    // Return a queued task through both editing views and native drag and drop.
    await card(id).click();
    await page.locator('#task-status').selectOption('BACKLOG');
    await screenshot('open-backlog-modal');
    await page.locator('#save-task').click();
    await expect(page.locator('#task-modal')).toBeHidden();
    await state(id, 'BACKLOG');
    assert.equal((await api('GET', `/${id}`)).body.status, 'BACKLOG');
    assert.equal((await api('PATCH', `/${id}/move`, { status: 'OPEN', position: 0 })).status, 200);
    await state(id, 'OPEN');
    await card(id).dragTo(column('BACKLOG').locator('.task-list'));
    await state(id, 'BACKLOG');
    assert.equal((await api('GET', `/${id}`)).body.status, 'BACKLOG');
    await screenshot('open-backlog-board');
    assert.equal((await api('PATCH', `/${id}/move`, { status: 'OPEN', position: 0 })).status, 200);
    const taskPage = await context.newPage();
    taskPage.on('pageerror', error => errors.push(error.message));
    await taskPage.goto(`${info.url}/tasks/${id}`, { waitUntil: 'domcontentloaded' });
    await taskPage.locator('#task-status').selectOption('BACKLOG');
    await taskPage.locator('#save-task').click();
    await expect(taskPage.locator('#delete-task')).toBeVisible();
    await expect(taskPage.locator('#task-status')).toHaveValue('BACKLOG');
    await taskPage.reload({ waitUntil: 'domcontentloaded' });
    await expect(taskPage.locator('#task-status')).toHaveValue('BACKLOG');
    await taskPage.setViewportSize({ width: 375, height: 900 });
    assert.equal(await taskPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.BOARD_LIVE_SCREENSHOTS === '1') {
      await taskPage.screenshot({ path: path.join(root, 'board-live-open-backlog-mobile.png'), fullPage: true });
    }
    await taskPage.close();
    assert.equal((await api('PATCH', `/${id}/move`, { status: 'OPEN', position: 0 })).status, 200);
    await state(id, 'OPEN');
    const first = agent('claim');
    assert.equal(first.task_id, id);
    assert.equal(first.session_id, null);
    await state(id, 'IN PROGRESS');
    await expect(column('OPEN').locator('.column-count')).toHaveText('0');
    await expect(card(id).locator('.task-extra-status')).toHaveText('In progress');
    await expect(card(id).locator('.task-extra-status span')).toBeVisible();
    await expect(card(id).locator('.task-card-bar')).toHaveCSS('background-color', 'rgb(67, 137, 231)');
    await screenshot('progress');

    await card(id).click();
    await page.locator('#task-title').fill('Unsaved title must survive');
    await page.locator('#task-description').fill('Unsaved description must survive');
    await screenshot('modal');
    agent('success', first.id);
    // The modal covers the card; wait for its attached state and count instead of visibility.
    await expect(column('REVIEW').locator(`[data-task-id="${id}"]`)).toHaveCount(1, { timeout: 15000 });
    await expect(column('REVIEW').locator('.column-count')).toHaveText('1');
    await expect(column('IN PROGRESS').locator('.column-count')).toHaveText('0');
    await expect(page.locator('#task-modal')).toBeVisible();
    await expect(page.locator('#task-title')).toHaveValue('Unsaved title must survive');
    await expect(page.locator('#task-description')).toHaveValue('Unsaved description must survive');
    await page.locator('#cancel-task').click();
    await expect(card(id).locator('.task-extra-status')).toHaveCount(0);
    await expect(card(id).locator('.task-card-bar')).toHaveCSS('background-color', 'rgb(131, 201, 102)');
    assert.equal((await api('GET', `/${id}`)).body.title, 'Backlog edit 3');
    await screenshot('review');

    assert.equal((await api('PATCH', `/${id}/move`, { status: 'OPEN', position: 0 })).status, 200);
    await state(id, 'OPEN');
    const second = agent('claim');
    await state(id, 'IN PROGRESS');
    agent('failure', second.id);
    await state(id, 'WAIT');
    await expect(card(id)).toHaveClass(/is-error/);
    await expect(card(id).locator('.task-extra-status')).toHaveCount(0);
    await expect(column('IN PROGRESS').locator('.column-count')).toHaveText('0');
    assert.equal((await api('GET', `/${id}`)).body.is_error, true);
    await expect(card(id).locator('.task-card-bar')).toHaveCSS('background-color', 'rgb(241, 91, 91)');
    await screenshot('error');

    const chat = await api('POST', `/${id}/chat`, { comment: 'Resume after the agent error' });
    assert.equal(chat.status, 200);
    assert.equal(chat.body.status, 'OPEN');
    assert.deepEqual(chat.body.messages.map(({ role, text }) => ({ role, text })), [
      { role: 'agent', text: 'Live test agent failure' },
      { role: 'user', text: 'Resume after the agent error' }
    ]);
    assert.deepEqual((await api('GET', `/${id}/chat`)).body, chat.body);
    await state(id, 'OPEN');
    await expect(column('WAIT').locator('.column-count')).toHaveText('0');
    await card(id).click();
    await expect(page.locator('#task-modal')).toBeVisible();
    await expect(page.locator('body')).toContainText('Resume after the agent error');
    await page.locator('#cancel-task').click();

    await context.setOffline(true);
    // Chromium offline emulation can leave an already-open streaming socket alive.
    // Exercise the real page-restoration reconnect path while transport is offline.
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
    await expect(page.locator('.toast[role="alert"]')).toContainText('connection lost', { timeout: 15000 });
    const third = agent('claim');
    agent('success', third.id);
    await page.waitForTimeout(1100); // More than two server poll intervals; offline DOM must remain stale.
    await state(id, 'OPEN');
    await context.setOffline(false);
    await state(id, 'REVIEW');
    await expect(column('OPEN').locator('.column-count')).toHaveText('0');
    await expect(card(id)).not.toHaveClass(/is-error/);

    // Native drag lifecycle, while observing the actual SSE snapshot delivery.
    assert.equal((await api('PATCH', `/${id}/move`, { status: 'OPEN', position: 0 })).status, 200);
    await state(id, 'OPEN');
    await card(id).dispatchEvent('dragstart');
    await expect(card(id)).toHaveClass(/dragging/);
    await page.evaluate(() => {
      window.liveDragSnapshot = false;
      const observer = new EventSource('/api/board/events');
      window.liveDragObserver = observer;
      observer.addEventListener('board', event => {
        if (JSON.parse(event.data).tasks.some(task => task.status === 'IN PROGRESS')) window.liveDragSnapshot = true;
      });
    });
    const fourth = agent('claim');
    await page.waitForFunction(() => window.liveDragSnapshot);
    await page.waitForTimeout(1100);
    await state(id, 'OPEN');
    await expect(card(id)).toHaveClass(/dragging/);
    await card(id).dispatchEvent('dragend');
    await state(id, 'IN PROGRESS');
    await expect(column('OPEN').locator('.column-count')).toHaveText('0');
    await page.evaluate(() => window.liveDragObserver.close());
    agent('success', fourth.id);
    await state(id, 'REVIEW');
    assert.equal(navigations, 1, 'Live updates must not navigate/reload the Board');
    assert.deepEqual(errors, []);
    console.log('PASS: real FastAPI/SSE + cross-process claim/finish; OPEN to BACKLOG via both forms and drag/drop, mobile layout, cards, counts, indicator, error, unsaved modal, offline reconnect and drag deferral.');
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
