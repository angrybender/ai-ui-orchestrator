const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

const root = path.resolve(__dirname, '..');
const raw = `<img src=x onerror="window.htmlExecuted=true"> <script>window.htmlExecuted=true</script> & "quotes" 'quotes' &lt;tag&gt;`;
const description = `Task body\n</textarea><svg onload="window.htmlExecuted=true"></svg>\n${raw}`;
const comment = `${raw}\n\n**Markdown stays bold**\n\n\`<b>code &amp;</b>\`\n\n\`\`\`html\n${raw}\n\`\`\`\n\n| Key | Value |\n| --- | --- |\n| HTML | safe |\n\n[unsafe](javascript:alert%281%29) [encoded](jav&#x61;script:alert%281%29) [control](java&#9;script:alert%281%29) [data](data:text/html,bad) [vb](vbscript:bad) [safe](https://example.com/?a=1&b=2) [relative](/docs) [email](mailto:test@example.com)\n\n![unsafe image](data:image/svg+xml,bad)`;

(async () => {
  const server = spawn(process.env.PYTHON || path.join(root, 'dev-venv/bin/python'),
    ['-u', path.join(__dirname, 'board_live_server.py')], {
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
    const context = await browser.newContext({ viewport: { width: 1459, height: 950 }, permissions: ['clipboard-read', 'clipboard-write'] });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await expect.poll(async () => {
      try { return (await context.request.get(info.url + '/health')).status(); } catch (_) { return 0; }
    }).toBe(200);

    await page.goto(info.url);
    await page.locator('#new-task-button').click();
    await page.locator('#task-id').fill('HTML-1');
    await page.locator('#task-title').fill(raw);
    await page.locator('#task-description').fill(description);
    await page.locator('#save-task').click();
    await expect(page.locator('#task-modal')).toBeHidden();
    const card = page.locator('.task-card[data-task-id="HTML-1"]');
    await expect(card.locator('.task-title')).toHaveText(raw);
    assert.equal(await card.locator('img, script, svg').count(), 0);
    if (process.env.HTML_ESCAPE_SCREENSHOTS) await page.screenshot({ path: `${process.env.HTML_ESCAPE_SCREENSHOTS}-board.png` });

    const checkStored = async (title, body) => {
      const tasks = await (await context.request.get(info.url + '/api/board/tasks')).json();
      const task = tasks.tasks.find(task => task.task_id === 'HTML-1');
      assert.equal(task.title, title);
      assert.equal(task.description, body);
      const chat = await (await context.request.get(info.url + '/api/board/tasks/HTML-1/chat')).json();
      chat.messages.forEach(message => assert.equal(message.text, comment));
    };
    const checkChat = async () => {
      const body = page.locator('.chat-user .chat-message-body').last();
      await expect(body.locator('strong')).toHaveText('Markdown stays bold');
      await expect(body.locator('p').first()).toContainText('<script>window.htmlExecuted=true</script>');
      await expect(body.locator('code').first()).toHaveText('<b>code &amp;</b>');
      await expect(body.locator('pre code')).toHaveText(raw + '\n');
      await expect(body.locator('table tbody td').last()).toHaveText('safe');
      assert.equal(await body.locator('script, svg, iframe, img[src="x"], [onerror], [onload]').count(), 0);
      for (const label of ['unsafe', 'encoded', 'control', 'data', 'vb']) {
        assert.equal(await body.locator('a').filter({ hasText: new RegExp(`^${label}$`) }).getAttribute('href'), null);
      }
      assert.equal(await body.getByRole('link', { name: 'safe', exact: true }).getAttribute('href'), 'https://example.com/?a=1&b=2');
      assert.equal(await body.getByRole('link', { name: 'relative', exact: true }).getAttribute('href'), '/docs');
      assert.equal(await body.getByRole('link', { name: 'email', exact: true }).getAttribute('href'), 'mailto:test@example.com');
      assert.equal(await body.locator('img').getAttribute('src'), null);
      assert.equal(await page.evaluate(() => window.htmlExecuted), undefined);
    };

    for (const standalone of [false, true]) {
      await page.goto(info.url + (standalone ? '/tasks/HTML-1' : '/'));
      if (!standalone) await card.click();
      await expect(page.locator('#task-title')).toHaveValue(raw);
      await expect(page.locator('#task-description')).toHaveValue(description);
      // Repeated saves must not convert input to entities in storage or double-escape it.
      await page.locator('#task-title').fill(raw + ' edited');
      await page.locator('#task-description').fill(description + '\nedited');
      await page.locator('#save-task').click();
      await expect(page.locator('#save-task')).toBeEnabled();
      await checkStored(raw + ' edited', description + '\nedited');
      await page.locator('#task-title').fill(raw);
      await page.locator('#task-description').fill(description);
      await page.locator('#save-task').click();
      await expect(page.locator('#save-task')).toBeEnabled();
      await page.locator('#chat-comment').fill(comment);
      await page.locator('#send-comment').click();
      await expect(page.locator('#chat-comment')).toHaveValue('');
      await checkChat();
      await page.locator('.chat-user').last().getByRole('button', { name: 'Copy message as Markdown' }).click();
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), comment);
      await page.reload();
      if (!standalone) await card.click();
      await expect(page.locator('#task-title')).toHaveValue(raw);
      await expect(page.locator('#task-description')).toHaveValue(description);
      await checkChat();
      await checkStored(raw, description);
      for (const width of [1459, 375]) {
        await page.setViewportSize({ width, height: 950 });
        assert.equal(await page.locator('.task-dialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
        if (process.env.HTML_ESCAPE_SCREENSHOTS) await page.screenshot({ path: `${process.env.HTML_ESCAPE_SCREENSHOTS}-${standalone ? 'page' : 'modal'}-${width}.png`, fullPage: true });
      }
      await page.setViewportSize({ width: 1459, height: 950 });
    }

    // Previously saved comments use the same safe renderer in the archive.
    assert.equal((await context.request.patch(info.url + '/api/board/tasks/HTML-1/move', { data: { status: 'ARCHIVE', position: 0 } })).status(), 200);
    await page.goto(info.url + '/archive');
    await expect(page.locator('.archive-task span')).toHaveText(raw);
    assert.equal(await page.locator('.archive-task img, .archive-task script').count(), 0);
    await page.locator('.archive-task').click();
    await expect(page.locator('#task-title')).toHaveValue(raw);
    await expect(page.locator('#task-description')).toHaveValue(description);
    await checkChat();

    // Responses from agents and system diagnostics must also never execute HTML.
    await context.route('**/api/board/tasks/HTML-1/chat', route => route.fulfill({ json: {
      status: 'ARCHIVE', messages: ['agent', 'system'].map(role => ({ role, text: raw, updated_at: '2026-11-09 10:00' }))
    } }));
    await page.reload();
    await expect(page.locator('.chat-message-body')).toHaveCount(2);
    await expect(page.locator('.chat-message-body').first()).toContainText('<script>window.htmlExecuted=true</script>');
    await expect(page.locator('.chat-message-body').last()).toHaveText(raw);
    assert.equal(await page.locator('.chat-message-body script, .chat-message-body img, .chat-message-body svg').count(), 0);
    assert.equal(await page.evaluate(() => window.htmlExecuted), undefined);

    // Missing Markdown support falls back to safe text.
    await page.addInitScript(() => { document.addEventListener('DOMContentLoaded', () => { window.marked = null; }); });
    await page.reload();
    await expect(page.locator('.chat-message-body').first()).toHaveText(raw);
    assert.deepEqual(errors, []);
    console.log('PASS: HTML is inert in title, description, comments, agent/system messages and archive; raw API round trips, repeat saves, Markdown/code/tables, URL schemes, copy, fallback and desktop/mobile layout.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const exited = new Promise(resolve => server.once('exit', resolve));
      server.stdin.end('\n');
      const timer = setTimeout(() => server.kill('SIGKILL'), 10000);
      await exited;
      clearTimeout(timer);
    }
    if (info) assert.equal(fs.existsSync(info.directory), false);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
