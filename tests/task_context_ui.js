const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const readline = require('node:readline');
const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || path.join(root, 'dev-venv/bin/python');
const fixture = path.join(__dirname, 'board_live_server.py');

(async () => {
  const server = spawn(python, ['-B', '-u', fixture], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
  let browser, info, log = '';
  server.stderr.on('data', data => { log += data; });
  try {
    info = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(log || 'Fixture timeout')), 15000);
      readline.createInterface({ input: server.stdout }).once('line', line => {
        clearTimeout(timer);
        resolve(JSON.parse(line));
      });
      server.once('error', reject);
    });
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', message => {
      // Rejected reset is intentionally exercised below.
      if (message.type() === 'error' && !message.text().includes('409 (Conflict)')) errors.push(message.text());
    });
    const operation = (name, id, text) => JSON.parse(execFileSync(python, ['-B', fixture,
      '--directory', info.directory, '--operation', name, '--run-id', id || '',
      ...(text === undefined ? [] : ['--message-text', text])], { cwd: root, encoding: 'utf8' }));
    await expect.poll(async () => {
      try { return (await page.request.get(info.url + '/health')).status(); } catch (_) { return 0; }
    }).toBe(200);
    for (const width of [1459, 375, 320]) {
      await page.setViewportSize({ width, height: 950 });
      for (const form of ['modal', 'page']) {
        const id = `CLEAR-${width}-${form}`;
        const base = info.url + `/api/board/tasks/${id}`;
        assert.equal((await page.request.post(info.url + '/api/board/tasks', {
          data: { task_id: id, title: 'Start with a fresh agent context', description: 'Keep task files and conversation history.' }
        })).status(), 201);
        const open = async () => {
          await page.goto(info.url + (form === 'page' ? `/tasks/${id}` : '/'));
          if (form === 'modal') await page.locator(`.task-card[data-task-id="${id}"]`).click();
          await expect(page.locator('#chat-composer')).toBeVisible();
        };
        await open();
        await expect(page.locator('#clear-context')).toBeHidden();
        await page.request.patch(base + '/move', { data: { status: 'OPEN', position: 0 } });
        const run = operation('claim');
        operation('session', run.id, 'previous-session');
        operation('message', run.id, 'Original answer stays in history');
        operation('success', run.id);
        await open();
        const clear = page.locator('#clear-context');
        await expect(clear).toBeVisible();
        const original = (await (await page.request.get(base + '/chat')).json()).messages;
        await page.fill('#task-title', 'Unsaved title');
        await page.fill('#chat-comment', 'Unsaved comment');
        await page.selectOption('#task-status', 'DONE');
        await expect(clear).toBeVisible(); // Saved status controls availability.
        let pending, resetRequests = 0;
        await page.route(base + '/clear-context', route => { resetRequests++; pending = route; });
        await clear.click();
        await expect(clear).toHaveText('Clearing…');
        await expect(clear.locator('.spinner')).toBeVisible();
        await expect(page.locator('#save-task')).toBeDisabled();
        await clear.dispatchEvent('click');
        if (form === 'modal') {
          await page.keyboard.press('Escape');
          await page.locator('#close-task').click();
          await expect(page.locator('#task-modal')).toBeVisible();
        }
        await expect.poll(() => resetRequests).toBe(1);
        await pending.fulfill({ status: 409, json: { detail: 'Agent is still stopping' } });
        await expect(page.getByRole('alert').filter({ hasText: 'Agent is still stopping' })).toBeVisible();
        await expect(clear).toBeEnabled();
        await expect(clear).toHaveText('Clear context');
        await expect(page.locator('#chat-comment')).toHaveValue('Unsaved comment');
        await expect(page.locator('#task-title')).toHaveValue('Unsaved title');
        await clear.click();
        await expect.poll(() => resetRequests).toBe(2);
        await pending.continue();
        await expect(clear).toBeHidden();
        await expect(page.locator('#save-task')).toBeEnabled();
        await expect(page.locator('.chat-message-body')).toHaveText([
          'Original answer stays in history', 'Agent context cleared. The next run will start a new session.'
        ]);
        await expect(page.locator('#chat-comment')).toHaveValue('Unsaved comment');
        await expect(page.locator('#task-title')).toHaveValue('Unsaved title');
        const saved = await (await page.request.get(base)).json();
        assert.equal(saved.status, 'REVIEW');
        assert.equal(saved.title, 'Start with a fresh agent context');
        assert.deepEqual((await (await page.request.get(base + '/chat')).json()).messages.slice(0, 1), original);
        await page.unroute(base + '/clear-context');
        if (process.env.TASK_CONTEXT_SCREENSHOTS) {
          fs.mkdirSync(process.env.TASK_CONTEXT_SCREENSHOTS, { recursive: true });
          await page.screenshot({ path: path.join(process.env.TASK_CONTEXT_SCREENSHOTS, `${width}-${form}-cleared.png`), fullPage: true });
        }
        await open();
        await expect(clear).toBeHidden();
        assert.equal((await page.request.post(base + '/chat', { data: { comment: 'Verify the existing files' } })).status(), 200);
        const fresh = operation('claim');
        assert.equal(fresh.session_id, null);
        assert.equal(fresh.comment, 'Verify the existing files');
        operation('session', fresh.id, 'fresh-session');
        await expect(page.locator('#chat-composer')).toBeHidden();
        operation('success', fresh.id);
        await expect(clear).toBeVisible();
        if (process.env.TASK_CONTEXT_SCREENSHOTS) {
          await clear.scrollIntoViewIfNeeded();
          await page.screenshot({ path: path.join(process.env.TASK_CONTEXT_SCREENSHOTS, `${width}-${form}-button.png`), fullPage: true });
        }
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        assert(await page.locator('.task-dialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1));
      }
    }
    // Creating a task never shows the reset action.
    await page.goto(info.url);
    await page.locator('#new-task-button').click();
    await expect(page.locator('#task-chat')).toBeHidden();
    assert.deepEqual(errors, []);
    console.log('PASS: Clear context in both forms at 1459/375/320px; real API, history and drafts preserved, spinner, duplicate guard, error recovery, reload, fresh claim and later reset.');
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
