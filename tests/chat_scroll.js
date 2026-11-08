const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const readline = require('node:readline');
const root = path.resolve(__dirname, '..');
(async () => {
  const server = spawn(process.env.PYTHON || path.join(root, 'dev-venv/bin/python'), ['-u', path.join(__dirname, 'board_live_server.py')], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
  let browser, info, log = '';
  server.stderr.on('data', data => { log += data; });
  try {
    info = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(log || 'Fixture timeout')), 15000);
      readline.createInterface({ input: server.stdout }).once('line', line => { clearTimeout(timer); resolve(JSON.parse(line)); });
      server.once('error', reject);
    });
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1459, height: 950 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await expect.poll(async () => {
      try { return (await page.request.get(info.url + '/health')).status(); } catch (_) { return 0; }
    }).toBe(200);
    assert.equal((await page.request.post(info.url + '/api/board/tasks', { data: { task_id: 'SCROLL-1', title: 'Scrollable conversation', description: 'Task fields stay in place' } })).status(), 201);
    let messages;
    await page.context().route('**/api/board/tasks/SCROLL-1/chat', route => route.fulfill({ json: { status: 'BACKLOG', messages } }));
    const message = (text, updated_at = '2026-10-17 12:00') => ({ role: 'agent', text, updated_at });
    const geometry = () => page.evaluate(() => {
      const dialog = document.querySelector('.task-dialog'), chat = document.querySelector('#chat-messages');
      return {
        height: dialog.getBoundingClientRect().height,
        top: dialog.getBoundingClientRect().top,
        outerScroll: dialog.scrollTop,
        outerOverflow: dialog.scrollHeight - dialog.clientHeight,
        chatScroll: chat.scrollTop,
        chatOverflow: chat.scrollHeight - chat.clientHeight,
        pageScroll: window.scrollY,
        titleTop: document.querySelector('#task-title').getBoundingClientRect().top
      };
    });
    for (const standalone of [false, true]) {
      for (const width of [1459, 701, 700, 375, 320]) {
        await page.setViewportSize({ width, height: 950 });
        messages = Array.from({ length: 40 }, (_, index) => ({
          ...message(`Saved message ${index + 1}`),
          role: index % 2 ? 'user' : 'system'
        }));
        await page.goto(info.url + (standalone ? '/tasks/SCROLL-1' : '/'));
        if (!standalone) await page.locator('.task-card[data-task-id="SCROLL-1"]').click();
        await expect(page.locator('.chat-message-body').last()).toHaveText('Saved message 40');
        const expectBottom = () => expect.poll(() => page.locator('#chat-messages').evaluate(node =>
          node.scrollHeight - node.clientHeight - node.scrollTop
        )).toBeLessThanOrEqual(1);
        await expectBottom();
        if (process.env.CHAT_SCROLL_SCREENSHOTS && [1459, 375].includes(width)) {
          await page.screenshot({ path: `${process.env.CHAT_SCROLL_SCREENSHOTS}-${standalone ? 'page' : 'modal'}-${width}.png`, fullPage: true });
        }
        const opened = await geometry();
        assert(opened.chatOverflow > 0, 'Saved history must overflow');
        assert.equal(opened.outerScroll, 0, 'Opening history must not scroll dialog');
        assert.equal(opened.pageScroll, 0, 'Opening history must not scroll page');
        const comment = page.locator('#chat-comment');
        const commentSize = () => comment.evaluate(node => {
          const style = getComputedStyle(node);
          return {
            height: node.getBoundingClientRect().height,
            minimum: 2 * parseFloat(style.lineHeight) + parseFloat(style.paddingTop) +
              parseFloat(style.paddingBottom) + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth),
            overflow: node.scrollHeight - node.clientHeight,
            overflowY: style.overflowY
          };
        });
        const compact = await commentSize();
        assert(Math.abs(compact.height - compact.minimum) < 1, 'Comment starts at two lines');
        await comment.fill('Line one\nLine two\nLine three\nLine four');
        const growing = await commentSize();
        assert(growing.height > compact.height && growing.height < 170, 'Comment grows with text');
        assert(growing.overflow <= 1, 'Growing comment does not scroll yet');
        await comment.fill('Long comment line\n'.repeat(40));
        const capped = await commentSize();
        assert.equal(capped.height, 170, 'Comment is capped at its previous height');
        assert(capped.overflow > 0 && capped.overflowY === 'auto', 'Long comment scrolls internally');
        await comment.fill('Short');
        assert(Math.abs((await commentSize()).height - compact.height) < 1, 'Deleting text shrinks comment');
        await comment.fill('Long comment line\n'.repeat(40));
        await page.locator('#send-comment').click();
        await expect(comment).toHaveValue('');
        await expect.poll(async () => (await commentSize()).height).toBe(compact.height);
        await comment.fill('Reopened comment\n'.repeat(40));
        await page.locator('#chat-messages').evaluate(node => { node.scrollTop = 0; });
        if (standalone) {
          await page.reload();
        } else {
          await page.locator('#cancel-task').click();
          await page.locator('.task-card[data-task-id="SCROLL-1"]').click();
        }
        await expect(page.locator('.chat-message-body').last()).toHaveText('Saved message 40');
        await expectBottom();
        assert(Math.abs((await commentSize()).height - compact.height) < 1, 'Reopening resets comment height');
        messages = [message('Short response')];
        await expect(page.locator('.chat-message-body')).toHaveText(['Short response']);
        const before = await geometry();
        messages = [message('Long response\n\n'.repeat(200), '2026-10-17 12:01')];
        await expect(page.locator('.chat-message-body')).toContainText('Long response');
        await expect.poll(async () => (await geometry()).chatScroll).toBeGreaterThan(0);
        const after = await geometry();
        assert(Math.abs(after.height - before.height) < 2, `Dialog grew at ${width}: ${JSON.stringify({ before, after })}`);
        assert.equal(after.outerScroll, before.outerScroll, 'Agent update scrolled dialog');
        assert.equal(after.pageScroll, before.pageScroll, 'Agent update scrolled page');
        assert(Math.abs(after.titleTop - before.titleTop) < 2, 'Task fields moved');
        assert(after.chatOverflow > 1000, 'History must overflow inside chat');
        if (width > 700) {
          assert(after.outerOverflow <= 1, 'Chat must not cause dialog overflow');
          await expect(page.locator('#send-comment')).toBeInViewport();
          await expect(page.locator('#save-task')).toBeInViewport();
        }
        await page.locator('#chat-messages').evaluate(node => { node.scrollTop = 0; });
        await page.waitForResponse(response => response.url().endsWith('/SCROLL-1/chat'));
        assert.equal((await geometry()).chatScroll, 0, 'Unchanged polling must preserve reading position');
      }
    }
    await page.setViewportSize({ width: 1459, height: 950 });
    await page.locator('#chat-comment').fill('Wrapped text '.repeat(25));
    const wideHeight = await page.locator('#chat-comment').evaluate(node => node.clientHeight);
    await page.setViewportSize({ width: 375, height: 950 });
    await expect.poll(() => page.locator('#chat-comment').evaluate(node => node.clientHeight)).toBeGreaterThan(wideHeight);
    await page.setViewportSize({ width: 1459, height: 950 });
    await expect.poll(() => page.locator('#chat-comment').evaluate(node => node.clientHeight)).toBe(wideHeight);
    messages = Array.from({ length: 40 }, (_, index) => message(`Tab message ${index + 1}`));
    await page.goto(info.url + '/');
    const popupPromise = page.waitForEvent('popup');
    await page.locator('.task-card[data-task-id="SCROLL-1"]').click({ modifiers: ['Control'] });
    const popup = await popupPromise;
    popup.on('pageerror', error => errors.push(error.message));
    await expect(popup).toHaveURL(info.url + '/tasks/SCROLL-1');
    await expect(popup.locator('.chat-message-body').last()).toHaveText('Tab message 40');
    await expect.poll(() => popup.locator('#chat-messages').evaluate(node =>
      node.scrollHeight - node.clientHeight - node.scrollTop
    )).toBeLessThanOrEqual(1);
    await popup.close();
    assert.deepEqual(errors, []);
    console.log('PASS: saved history scrolls to bottom on open/reopen/reload; long chat stays within history; fixed form geometry, local autoscroll, unchanged polling and desktop/mobile in both task views');
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
