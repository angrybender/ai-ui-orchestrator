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
    const operation = (name, id, text) => JSON.parse(execFileSync(python, ['-B', fixture,
      '--directory', info.directory, '--operation', name, '--run-id', id || '',
      ...(text === undefined ? [] : ['--message-text', text])], { cwd: root, encoding: 'utf8' }));
    await expect.poll(async () => {
      try { return (await page.request.get(info.url + '/health')).status(); } catch (_) { return 0; }
    }).toBe(200);
    for (const width of [1459, 375]) {
      await page.setViewportSize({ width, height: 900 });
      for (const form of ['modal', 'page']) {
        const id = `TURN-${width}-${form}`;
        assert.equal((await page.request.post(info.url + '/api/board/tasks', {
          multipart: { task_id: id, title: 'Background result verification', description: 'Keep the first answer and add each verification answer.' }
        })).status(), 201);
        assert.equal((await page.request.patch(info.url + `/api/board/tasks/${id}/move`, {
          data: { status: 'OPEN', position: 0 }
        })).status(), 200);
        const run = operation('claim');
        operation('agent', run.id);
        operation('message', run.id, 'Original answer');
        const chatUrl = info.url + `/api/board/tasks/${id}/chat`;
        const chat = async () => (await (await page.request.get(chatUrl)).json()).messages;
        const first = (await chat())[0];
        const open = async () => {
          if (form === 'modal') {
            await page.goto(info.url);
            await page.locator(`.task-card[data-task-id="${id}"]`).click();
          } else {
            await page.goto(info.url + `/tasks/${id}`);
          }
        };
        await open();
        const bodies = page.locator('.chat-message-body');
        await expect(bodies).toHaveText(['Original answer']);
        operation('next-message', run.id);
        assert.deepEqual(await chat(), [first]);
        operation('message', run.id, 'Checking background');
        await expect(bodies).toHaveText(['Original answer', 'Checking background']);
        const secondId = (await chat())[1].id;
        operation('message', run.id, 'Background results verified');
        await expect(bodies).toHaveText(['Original answer', 'Background results verified']);
        assert.equal((await chat())[1].id, secondId);
        assert.deepEqual((await chat())[0], first);
        operation('next-message', run.id);
        operation('message', run.id, 'Background results verified');
        operation('success', run.id);
        const expected = ['Original answer', 'Background results verified', 'Background results verified'];
        await expect(bodies).toHaveText(expected);
        const saved = await chat();
        assert.equal(new Set(saved.map(m => m.id)).size, 3);
        assert.deepEqual(new Set(saved.map(m => m.run_id)), new Set([run.id]));
        assert.deepEqual(saved[0], first);
        await page.reload();
        if (form === 'modal') await page.locator(`.task-card[data-task-id="${id}"]`).click();
        await expect(bodies).toHaveText(expected);
        await expect(page.locator('.chat-message-time')).toHaveCount(3);
        assert.deepEqual(await chat(), saved);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        if (process.env.CHAT_TURNS_SCREENSHOTS) {
          await bodies.last().scrollIntoViewIfNeeded();
          fs.mkdirSync(process.env.CHAT_TURNS_SCREENSHOTS, { recursive: true });
          await page.screenshot({ path: path.join(process.env.CHAT_TURNS_SCREENSHOTS, `${width}-${form}.png`), fullPage: true });
        }
      }
    }
    assert.deepEqual(errors, []);
    console.log('PASS: separate prompt responses, streaming in place, unchanged original IDs/dates, identical replies, same run_id, persistent real API, both forms at 1459/375px');
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
