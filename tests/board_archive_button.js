const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch();
  try {
    const root = path.resolve(__dirname, '..');
    const template = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
    const modal = template.slice(template.indexOf('<div class="modal-backdrop"'), template.indexOf('{% endblock %}', template.indexOf('<div class="modal-backdrop"')));
    const statuses = ['BACKLOG', 'OPEN', 'WAIT', 'IN PROGRESS', 'REVIEW', 'DONE', 'ARCHIVE'];
    const tasks = statuses.map((status, index) => ({ task_id: `TEST-${index}`, title: status, description: 'Test task', status, attachments: [] }));
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    const errors = [];
    const moves = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://board.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/move')) {
        moves.push(route.request().postDataJSON());
        return route.fulfill({ json: { tasks: [] } });
      }
      if (url.pathname.endsWith('/next-id')) return route.fulfill({ json: { task_id: 'TEST-NEW' } });
      if (url.pathname === '/api/board/tasks') return route.fulfill({ json: { tasks } });
      return route.fulfill({ contentType: 'text/html', body: '<html><body></body></html>' });
    });
    for (const standalone of [false, true]) {
      for (const task of tasks) {
        await page.goto('http://board.test/');
        await page.setContent((standalone ? '' : '<div id="board"></div>') + '<button id="new-task-button">New task</button>' + `<div class="task-list" data-status="${task.status}"></div>` + modal);
        await page.addStyleTag({ path: path.join(root, 'static/css/app.css') });
        await page.addStyleTag({ path: path.join(root, 'static/css/board.css') });
        await page.evaluate(id => { window.openTaskId = id; window.showToast = () => {}; }, task.task_id);
        await page.addScriptTag({ path: path.join(root, 'static/js/board.js') });
        await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
        await page.locator('#task-modal').waitFor({ state: 'visible' });
        const button = page.locator('#archive-task');
        assert.equal(await button.isVisible(), task.status === 'DONE', `${task.status}, standalone=${standalone}`);
        if (task.status === 'DONE') {
          assert.equal(await button.isEnabled(), true);
          if (!standalone) {
            await button.click();
            await page.locator('#task-modal').waitFor({ state: 'hidden' });
            assert.deepEqual(moves.pop(), { status: 'ARCHIVE', position: 0 });
          }
        } else {
          await button.dispatchEvent('click');
          assert.equal(moves.length, 0);
        }
      }
    }
    await page.locator('#close-task').click();
    await page.locator('#new-task-button').click();
    await page.waitForFunction(() => document.querySelector('#task-mode').value === 'create');
    assert.equal(await page.locator('#archive-task').isVisible(), false);
    if (process.env.BOARD_TEST_SCREENSHOT) await page.screenshot({ path: process.env.BOARD_TEST_SCREENSHOT });
    assert.deepEqual(errors, []);
    console.log('PASS: To Archive visible only for DONE across 7 statuses in modal/standalone layouts; creation hidden; DONE archive request verified.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
