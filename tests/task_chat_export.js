const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const readline = require('node:readline');

const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || path.join(root, 'dev-venv/bin/python');
const fixture = path.join(__dirname, 'board_live_server.py');
const title = 'Экспорт <script>window.exportExecuted=true</script> & "чат"';
const markdown = '**Жирный текст**\n\n`<b>code &amp;</b>`\n\n```html\n<script>window.exportExecuted=true</script>\n```\n\n| Key | Value |\n| --- | ---: |\n| HTML | safe |\n\n<script>window.exportExecuted=true</script>\n\n[unsafe](javascript:alert%281%29) [relative](/docs) [safe](https://example.com/?a=1&b=2)\n\n![Диаграмма](https://example.com/diagram.png)';

(async () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'chat-export-'));
  const server = spawn(python, ['-B', '-u', fixture], {
    cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, stdio: ['pipe', 'pipe', 'pipe']
  });
  let browser, info, log = '';
  server.stderr.on('data', data => { log += data; });
  try {
    info = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(log || 'Fixture startup timeout')), 15000);
      readline.createInterface({ input: server.stdout }).once('line', line => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${log}`)); });
    });
    browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1459, height: 950 }, acceptDownloads: true });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    // The live chat still renders Markdown images; their network is irrelevant to the export.
    await context.route('https://example.com/diagram.png', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"/>' }));
    await expect.poll(async () => {
      try { return (await context.request.get(info.url + '/health')).status(); } catch (_) { return 0; }
    }).toBe(200);
    const base = info.url + '/api/board/tasks/EXPORT-1';
    const operation = (name, id, text) => JSON.parse(execFileSync(python, ['-B', fixture,
      '--directory', info.directory, '--operation', name, '--run-id', id || '',
      ...(text === undefined ? [] : ['--message-text', text])], { cwd: root, encoding: 'utf8' }));
    assert.equal((await context.request.post(info.url + '/api/board/tasks', {
      data: { task_id: 'EXPORT-1', title, description: 'Полная история чата, включая ранние сообщения.' }
    })).status(), 201);
    for (let index = 1; index <= 15; index++) {
      assert.equal((await context.request.post(base + '/chat', { data: { comment: `Сообщение ${index}` } })).status(), 200);
    }
    assert.equal((await context.request.post(base + '/chat', { data: { comment: markdown } })).status(), 200);
    assert.equal((await context.request.patch(base + '/move', { data: { status: 'OPEN', position: 0 } })).status(), 200);
    const run = operation('claim');
    operation('message', run.id, 'Первый ответ агента');
    operation('next-message', run.id);
    operation('message', run.id, 'Второй ответ агента');
    operation('init-failure', run.id);

    async function verifyDownload(expected, filename) {
      const downloadPromise = page.waitForEvent('download');
      await page.getByRole('button', { name: 'Export HTML', exact: true }).click();
      const download = await downloadPromise;
      assert.equal(download.suggestedFilename(), 'EXPORT-1-chat.html');
      const output = path.join(temporary, filename);
      await download.saveAs(output);
      assert.equal(await download.failure(), null);
      const html = fs.readFileSync(output, 'utf8');
      assert(html.startsWith('<!doctype html>'));
      const offlineContext = await browser.newContext({ offline: true, viewport: { width: 375, height: 701 } });
      try {
        const offline = await offlineContext.newPage();
        const requests = [];
        offline.on('pageerror', error => errors.push(error.message));
        offline.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
        offline.on('request', request => { if (/^https?:/.test(request.url())) requests.push(request.url()); });
        await offline.goto(pathToFileURL(output).href);
        await expect(offline.locator('h1')).toHaveText(`EXPORT-1 — ${title}`);
        await expect(offline.locator('article')).toHaveCount(expected.messages.length);
        assert.deepEqual(await offline.locator('article time').allTextContents(), expected.messages.map(message => message.updated_at));
        assert.deepEqual(await offline.locator('.message-meta strong').allTextContents(), expected.messages.map(message => ({ user: 'User', agent: 'Agent', system: 'System' }[message.role])));
        await expect(offline.locator('article').first().locator('.message-body')).toHaveText('Сообщение 1');
        await expect(offline.locator('.message-body strong')).toHaveText('Жирный текст');
        await expect(offline.locator('pre code')).toHaveText('<script>window.exportExecuted=true</script>\n');
        await expect(offline.locator('tbody td').last()).toHaveText('safe');
        await expect(offline.locator('.plain-text')).toHaveText(expected.messages.filter(message => message.role === 'system').map(message => message.text));
        assert.equal(await offline.locator('script, link, img, iframe, button, [onerror], [onload]').count(), 0);
        assert.equal(await offline.locator('a').filter({ hasText: /^unsafe$/ }).getAttribute('href'), null);
        assert.equal(await offline.getByRole('link', { name: 'relative', exact: true }).getAttribute('href'), info.url + '/docs');
        assert.equal(await offline.getByRole('link', { name: 'Диаграмма', exact: true }).getAttribute('href'), 'https://example.com/diagram.png');
        assert.equal(await offline.evaluate(() => window.exportExecuted), undefined);
        assert(await offline.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        assert.deepEqual(requests, [], 'Export must open offline without resource requests');
        if (process.env.CHAT_EXPORT_SCREENSHOTS) {
          fs.mkdirSync(process.env.CHAT_EXPORT_SCREENSHOTS, { recursive: true });
          await offline.screenshot({ path: path.join(process.env.CHAT_EXPORT_SCREENSHOTS, 'export-offline.png'), fullPage: true });
        }
      } finally { await offlineContext.close(); }
    }

    for (const width of [1459, 700, 375, 320]) {
      await page.setViewportSize({ width, height: 950 });
      for (const form of ['modal', 'page']) {
        await page.goto(info.url + (form === 'page' ? '/tasks/EXPORT-1' : '/'));
        if (form === 'modal') await page.locator('.task-card[data-task-id="EXPORT-1"]').click();
        await expect(page.getByRole('button', { name: 'Export HTML', exact: true })).toBeEnabled();
        await page.locator('#task-title').fill('Несохранённый заголовок');
        await page.locator('#chat-comment').fill('Несохранённый комментарий');
        const before = await (await context.request.get(base + '/chat')).json();
        assert(await page.locator('#chat-messages').evaluate(node => node.scrollHeight > node.clientHeight));
        await verifyDownload(before, `${width}-${form}.html`);
        await expect(page.locator('#task-title')).toHaveValue('Несохранённый заголовок');
        await expect(page.locator('#chat-comment')).toHaveValue('Несохранённый комментарий');
        assert.deepEqual(await (await context.request.get(base + '/chat')).json(), before);
        assert(await page.locator('.task-dialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1));
        if (width > 700) {
          await expect(page.locator('#export-chat')).toBeInViewport();
          await expect(page.locator('#send-comment')).toBeInViewport();
          assert(await page.locator('.task-dialog').evaluate(node => node.scrollHeight <= node.clientHeight + 1));
        }
        if (process.env.CHAT_EXPORT_SCREENSHOTS) {
          if (form === 'page') await page.evaluate(() => window.scrollTo(0, 0));
          await page.screenshot({ path: path.join(process.env.CHAT_EXPORT_SCREENSHOTS, `${width}-${form}.png`), fullPage: true });
          if (width === 1459) {
            await page.setViewportSize({ width, height: 701 });
            await page.screenshot({ path: path.join(process.env.CHAT_EXPORT_SCREENSHOTS, `${width}-${form}-reference.png`), fullPage: true });
            await page.setViewportSize({ width, height: 950 });
          }
        }
      }
    }

    // A new polling snapshot must update the exported history, including a new agent turn.
    assert.equal((await context.request.post(base + '/chat', { data: { comment: 'Продолжить работу' } })).status(), 200);
    const nextRun = operation('claim');
    operation('message', nextRun.id, 'Последний ответ после обновления');
    operation('success', nextRun.id);
    await expect(page.locator('.chat-message-body').last()).toHaveText('Последний ответ после обновления');
    await verifyDownload(await (await context.request.get(base + '/chat')).json(), 'latest.html');

    // View-only modes keep the export outside the hidden comment composer.
    assert.equal((await context.request.patch(base + '/move', { data: { status: 'DONE', position: 0 } })).status(), 200);
    await expect(page.locator('#chat-composer')).toBeHidden();
    await verifyDownload(await (await context.request.get(base + '/chat')).json(), 'done.html');
    assert.equal((await context.request.patch(base + '/move', { data: { status: 'ARCHIVE', position: 0 } })).status(), 200);
    await page.goto(info.url + '/tasks/EXPORT-1');
    await expect(page.locator('#export-chat')).toBeEnabled();
    await expect(page.locator('#chat-composer')).toBeHidden();
    await verifyDownload(await (await context.request.get(base + '/chat')).json(), 'archive.html');

    assert.equal((await context.request.post(info.url + '/api/board/tasks', {
      data: { task_id: 'EMPTY-1', title: 'Пустой чат', description: 'Нет сообщений' }
    })).status(), 201);
    await page.goto(info.url + '/tasks/EMPTY-1');
    await expect(page.locator('#task-chat')).toBeVisible();
    await expect(page.locator('#export-chat')).toBeDisabled();
    await page.goto(info.url);
    await page.locator('#new-task-button').click();
    await expect(page.locator('#task-chat')).toBeHidden();
    assert.deepEqual(errors, []);
    console.log('PASS: HTML export in both forms at 1459/700/375/320px; full history, roles, dates, Markdown, Unicode, safe HTML, offline file://, current snapshots, DONE/ARCHIVE, empty/new task and preserved drafts/data.');
  } finally {
    if (browser) await browser.close();
    const exited = server.exitCode === null ? new Promise(resolve => server.once('exit', resolve)) : Promise.resolve();
    server.stdin.end('\n');
    const timer = setTimeout(() => server.kill('SIGKILL'), 10000);
    await exited;
    clearTimeout(timer);
    fs.rmSync(temporary, { recursive: true, force: true });
    if (info) assert.equal(fs.existsSync(info.directory), false);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
