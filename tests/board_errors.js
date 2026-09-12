const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch();
  try {
    const root = path.resolve(__dirname, '..');
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let pending;
    let saves = 0;
    let failLoad = false;
    const task = { task_id: 'TEST-1', title: 'Task', description: 'Description', status: 'DONE', attachments: [] };
    await page.route('http://board.test/**', route => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === 'PUT') { saves++; pending = route; return; }
      if (url.pathname.endsWith('/move')) return route.fulfill({ status: 502, json: { detail: 'Archive failed' } });
      if (url.pathname.endsWith('/next-id')) return route.fulfill({ status: 503, json: { detail: 'ID unavailable' } });
      if (url.pathname === '/api/board/tasks') return route.fulfill(failLoad
        ? { status: 503, json: { detail: 'Load failed' } }
        : { json: { tasks: [task] } });
      return route.fulfill({ contentType: 'text/html', body: '<html><body></body></html>' });
    });
    async function open() {
      await page.goto('http://board.test/');
      const source = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
      const start = source.indexOf('<div class="modal-backdrop"');
      await page.setContent('<div id="board"><div class="task-list" data-status="DONE"></div></div><button id="new-task-button">New task</button>' + source.slice(start, source.indexOf('{% endblock %}', start)));
      for (const name of ['app', 'board']) await page.addStyleTag({ path: path.join(root, `static/css/${name}.css`) });
      await page.evaluate(() => { window.messages = []; window.showToast = (text, options) => window.messages.push({ text, options }); });
      for (const name of ['spinner', 'board']) await page.addScriptTag({ path: path.join(root, `static/js/${name}.js`) });
      await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
    }
    await open();
    await page.locator('.task-card').click();
    for (const width of [320, 375, 768, 1280]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.equal(await page.locator('.task-dialog').evaluate(node => node.scrollWidth <= node.clientWidth), true, `Dialog overflow at ${width}px`);
    }
    await page.locator('#task-title').fill('Keep unsaved');
    await page.locator('#save-task').click();
    await page.waitForFunction(() => document.querySelector('#task-form').dataset.spinnerActive === 'true');
    await page.locator('#task-form').dispatchEvent('submit', { bubbles: true, cancelable: true });
    await page.locator('#close-task').click();
    assert.equal(await page.locator('#task-modal').isVisible(), true);
    await page.waitForTimeout(30);
    assert.equal(saves, 1);
    await pending.fulfill({ status: 422, json: { detail: { title: 'Invalid title' } } });
    await page.waitForFunction(() => window.messages.length > 0);
    assert.equal(await page.locator('[data-error="title"]').textContent(), 'Invalid title');
    assert.equal(await page.locator('#task-title').inputValue(), 'Keep unsaved');
    assert.equal(await page.locator('#save-task').isEnabled(), true);
    await page.locator('#archive-task').click();
    await page.waitForFunction(() => window.messages.some(item => item.text === 'Archive failed'));
    assert.equal(await page.locator('#task-modal').isVisible(), true);
    await page.locator('#cancel-task').click();
    await page.locator('#new-task-button').click();
    await page.waitForFunction(() => window.messages.some(item => item.text === 'ID unavailable'));
    assert.notEqual(await page.locator('#task-id').inputValue(), 'undefined');
    failLoad = true;
    await open();
    await page.waitForFunction(() => window.messages.some(item => item.text === 'Load failed'));
    assert.deepEqual(errors, []);
    console.log('PASS: save validation, duplicate submit, pending close guard, archive/load/next-ID errors and no unhandled rejections');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
