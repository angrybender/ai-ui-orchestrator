const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1459, height: 950 } });
    const root = path.resolve(__dirname, '..');
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let status = 'REVIEW', messages = [{ role: 'agent', text: 'Please review <script>unsafe</script>', updated_at: '2026-09-11 10:00' }], pending, requests = 0;
    const task = () => ({ task_id: 'CHAT-1', title: 'Task title', description: 'Task description', status, attachments: [] });
    await page.route('http://chat.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/chat')) {
        if (route.request().method() === 'POST') { requests++; pending = route; return; }
        return route.fulfill({ json: { status, messages } });
      }
      if (url.pathname === '/api/board/tasks') return route.fulfill({ json: { tasks: [task()] } });
      return route.fulfill({ contentType: 'text/html', body: '<html></html>' });
    });
    async function open(standalone) {
      await page.goto('http://chat.test/');
      let html = fs.readFileSync(path.join(root, 'templates', standalone ? 'task.html' : 'index.html'), 'utf8');
      const start = html.indexOf(standalone ? '<section class="task-page-editor"' : '<div class="modal-backdrop"');
      html = html.slice(start, html.indexOf('{% endblock %}', start)).replace(/{% if task.status == "ARCHIVE" %}[\s\S]*?{% else %}/g, '').replace(/{% endif %}/g, '').replace(/{{ task.task_id }}/g, 'CHAT-1');
      await page.setContent((standalone ? '' : '<div id="board"><div class="task-list" data-status="REVIEW"></div></div>') + html);
      for (const name of ['app', 'board', 'spinner']) await page.addStyleTag({ path: path.join(root, `static/css/${name}.css`) });
      await page.evaluate(standalone => { window.showToast = text => { window.lastToast = text; }; if (standalone) window.openTaskId = 'CHAT-1'; }, standalone);
      for (const name of ['spinner', 'task-chat', 'board']) await page.addScriptTag({ path: path.join(root, `static/js/${name}.js`) });
      await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
      if (!standalone) await page.locator('.task-card').click();
      await page.waitForSelector('.chat-agent');
    }
    for (const standalone of [false, true]) {
      status = 'REVIEW'; messages = [{ role: 'agent', text: 'Please review <script>unsafe</script>', updated_at: '2026-09-11 10:00' }];
      await open(standalone);
      assert.equal(await page.locator('.chat-agent span').textContent(), messages[0].text);
      assert.equal(await page.locator('.chat-agent time').textContent(), '2026-09-11 10:00');
      await page.fill('#task-title', 'Unsaved title');
      await page.fill('#chat-comment', 'Continue');
      messages[0].text = 'Latest response';
      messages[0].updated_at = '2026-09-11 10:01';
      await page.waitForFunction(() => document.querySelector('.chat-agent span').textContent === 'Latest response');
      assert.equal(await page.locator('.chat-agent').count(), 1);
      assert.equal(await page.locator('.chat-agent time').textContent(), '2026-09-11 10:01');
      assert.equal(await page.inputValue('#chat-comment'), 'Continue');
      await page.click('#send-comment');
      await page.waitForFunction(() => document.querySelector('#task-form').dataset.spinnerActive === 'true');
      assert.equal(await page.isDisabled('#save-task'), true);
      await page.locator('#send-comment').dispatchEvent('click');
      if (!standalone) { await page.click('#close-task'); assert.equal(await page.isVisible('#task-modal'), true); }
      await pending.fulfill({ status: 409, json: { detail: 'Rejected comment' } });
      await page.waitForFunction(() => !document.querySelector('#task-form').dataset.spinnerActive);
      assert.equal(await page.inputValue('#chat-comment'), 'Continue');
      assert.equal(await page.inputValue('#task-title'), 'Unsaved title');
      await page.click('#send-comment');
      await page.waitForTimeout(40);
      status = 'OPEN'; messages.push({ role: 'user', text: 'Continue', updated_at: '2026-09-11 10:02' });
      await pending.fulfill({ json: { status, messages } });
      await page.waitForSelector('.chat-user');
      assert.equal(await page.isVisible('#chat-composer'), false);
      assert.equal(await page.inputValue('#task-title'), 'Unsaved title');
      status = 'IN PROGRESS';
      messages.push({ role: 'agent', text: 'New session response', updated_at: '2026-09-11 10:03' });
      await page.waitForFunction(() => document.querySelectorAll('.chat-agent').length === 2);
      assert.deepEqual(await page.locator('.chat-message span').allTextContents(), ['Latest response', 'Continue', 'New session response']);
      messages[2].text = 'New session final response';
      messages[2].updated_at = '2026-09-11 10:04';
      await page.waitForFunction(() => document.querySelectorAll('.chat-agent span')[1].textContent === 'New session final response');
      assert.equal(await page.locator('.chat-agent').count(), 2);
      assert.equal(await page.locator('.chat-agent time').first().textContent(), '2026-09-11 10:01');
      for (const next of ['BACKLOG', 'OPEN', 'WAIT', 'IN PROGRESS', 'REVIEW', 'DONE', 'ARCHIVE']) {
        status = next;
        await page.waitForTimeout(650);
        assert.equal(await page.isVisible('#chat-composer'), ['REVIEW', 'WAIT'].includes(next));
        assert.equal(await page.isVisible('#task-chat'), true);
      }
      for (const width of [320, 700, 1459]) {
        await page.setViewportSize({ width, height: 950 });
        assert.equal(await page.locator('.task-dialog').evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
      }
      messages[2].text = 'Long response\n'.repeat(100);
      await page.waitForFunction(() => document.querySelectorAll('.chat-agent span')[1].textContent.startsWith('Long response'));
      const lastVisible = () => page.locator('.chat-message').last().evaluate(node => {
        const rect = node.getBoundingClientRect();
        return rect.bottom <= innerHeight + 2 && rect.bottom > 0;
      });
      assert.equal(await lastVisible(), true);
      const resetScroll = () => page.evaluate(() => {
        document.querySelectorAll('*').forEach(node => { if (node.scrollTop) node.scrollTop = 0; });
        window.scrollTo(0, 0);
      });
      await resetScroll();
      await page.waitForTimeout(700);
      assert.equal(await lastVisible(), false);
      messages.push({ role: 'user', text: 'No auto-scroll', updated_at: '2026-09-11 10:05' });
      await page.waitForFunction(() => document.querySelectorAll('.chat-user').length === 2);
      assert.equal(await lastVisible(), false);
      messages[2].updated_at = '2026-09-11 10:06';
      await page.waitForFunction(() => document.querySelectorAll('.chat-agent time')[1].textContent === '2026-09-11 10:06');
      assert.equal(await lastVisible(), true);
      if (process.env.CHAT_TEST_SCREENSHOT) await page.screenshot({ path: process.env.CHAT_TEST_SCREENSHOT });
    }
    assert.equal(requests, 4);
    assert.deepEqual(errors, []);
    console.log('PASS: chat in both templates, latest response, plain text, status visibility, unsaved drafts, POST failure/success, spinner, duplicate prevention, mobile layout.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
