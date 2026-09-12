const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch();
  try {
    const root = path.resolve(__dirname, '..');
    const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let task;
    let pending;
    const requests = [];
    await page.route('http://board.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/move')) {
        requests.push(route.request().postDataJSON());
        pending = route;
        return;
      }
      if (url.pathname.endsWith('/next-id')) return route.fulfill({ json: { task_id: 'TEST-NEW' } });
      if (url.pathname.startsWith('/api/')) return route.fulfill({ json: { tasks: [task] } });
      return route.fulfill({ contentType: 'text/html', body: '<html><body></body></html>' });
    });
    async function open(status, standalone) {
      task = { task_id: 'TEST-1', title: 'Review task', description: 'Description', status, attachments: [] };
      pending = null;
      requests.length = 0;
      await page.goto('http://board.test/');
      let html = fs.readFileSync(path.join(root, 'templates', standalone ? 'task.html' : 'index.html'), 'utf8');
      const start = html.indexOf(standalone ? '<section class="task-page-editor"' : '<div class="modal-backdrop"');
      html = html.slice(start, html.indexOf('{% endblock %}', start));
      html = html.replace(/{% if task.status == "ARCHIVE" %}[\s\S]*?{% else %}/g, '').replace(/{% endif %}/g, '').replace(/{{ task.task_id }}/g, 'TEST-1');
      const board = standalone ? '' : `<div id="board">${['BACKLOG', 'OPEN', 'WAIT', 'IN PROGRESS', 'REVIEW', 'DONE'].map(value => `<div class="task-list" data-status="${value}"></div>`).join('')}</div><button id="new-task-button">New task</button>`;
      await page.setContent(board + html);
      for (const file of ['app', 'board', 'spinner']) await page.addStyleTag({ path: path.join(root, `static/css/${file}.css`) });
      await page.evaluate(standalone => {
        window.messages = [];
        window.showToast = (text, options) => window.messages.push({ text, options });
        if (standalone) window.openTaskId = 'TEST-1';
      }, standalone);
      for (const file of ['spinner', 'board']) await page.addScriptTag({ path: path.join(root, `static/js/${file}.js`) });
      await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
      if (!standalone) await page.locator('.task-card').click();
      await page.waitForFunction(() => document.querySelector('#task-title').value === 'Review task');
    }
    for (const standalone of [false, true]) {
      for (const status of ['BACKLOG', 'OPEN', 'WAIT', 'IN PROGRESS', 'REVIEW', 'DONE', 'ARCHIVE']) {
        if (!standalone && status === 'ARCHIVE') continue;
        await open(status, standalone);
        for (const id of ['reopen-task', 'done-task']) assert.equal(await page.locator(`#${id}`).isVisible(), status === 'REVIEW');
      }
      for (const [id, destination] of [['reopen-task', 'OPEN'], ['done-task', 'DONE']]) {
        await open('REVIEW', standalone);
        if (process.env.REVIEW_TEST_SCREENSHOT && !standalone && destination === 'OPEN') await page.screenshot({ path: process.env.REVIEW_TEST_SCREENSHOT });
        await page.locator('#task-status').selectOption('BACKLOG');
        await page.locator('#task-title').fill('Unsaved title');
        await page.locator(`#${id}`).click();
        await page.waitForFunction(() => document.querySelector('#task-form').dataset.spinnerActive === 'true');
        assert.equal(await page.locator('#save-task').isDisabled(), true);
        await page.locator(`#${id}`).dispatchEvent('click');
        await page.waitForTimeout(50);
        assert.equal(requests.length, 1);
        assert.deepEqual(requests[0], { status: destination, position: 0 });
        task.status = destination;
        await pending.fulfill({ json: { tasks: [task] } });
        await page.waitForFunction(() => !document.querySelector('#task-form').dataset.spinnerActive);
        if (standalone) {
          assert.equal(await page.locator('#task-status').inputValue(), destination);
          assert.equal(await page.locator('#reopen-task').isVisible(), false);
          assert.equal(await page.locator('#done-task').isVisible(), false);
          assert.equal(await page.locator('#archive-task').isVisible(), destination === 'DONE');
          assert.equal(await page.locator('#task-title').inputValue(), 'Review task');
        } else {
          assert.equal(await page.locator('#task-modal').isVisible(), false);
          assert.equal(await page.locator(`.task-list[data-status="${destination}"] .task-card`).count(), 1);
        }
      }
      await open('REVIEW', standalone);
      await page.locator('#task-title').fill('Keep on error');
      await page.locator('#done-task').click();
      await page.waitForTimeout(50);
      await pending.fulfill({ status: 409, json: { detail: 'Transition rejected' } });
      await page.waitForFunction(() => window.messages.length > 0);
      assert.equal(await page.locator('#task-title').inputValue(), 'Keep on error');
      assert.equal(await page.locator('#done-task').isEnabled(), true);
      assert.equal(await page.locator('#done-task').isVisible(), true);
      assert.equal(await page.evaluate(() => window.messages[0].text), 'Transition rejected');
    }
    await open('REVIEW', false);
    await page.locator('#cancel-task').click();
    await page.locator('#new-task-button').click();
    await page.waitForFunction(() => document.querySelector('#task-mode').value === 'create');
    assert.equal(await page.locator('#reopen-task').isVisible(), false);
    assert.equal(await page.locator('#done-task').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log('PASS: REVIEW buttons visibility, OPEN/DONE transitions, spinner, duplicate prevention, errors, creation and both templates.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
