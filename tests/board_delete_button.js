const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// No server, database, SSH or external network is used: every request is routed.
(async () => {
  const browser = await chromium.launch();
  try {
    const root = path.resolve(__dirname, '..');
    const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
    const statuses = ['BACKLOG', 'OPEN', 'WAIT', 'IN PROGRESS', 'REVIEW', 'DONE'];
    const errors = [];
    const unexpected = [];
    const deletes = [];
    const confirmations = [];
    let tasks;
    let task;
    let pending;
    let accept = false;
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', async dialog => {
      confirmations.push({ type: dialog.type(), message: dialog.message() });
      if (accept) await dialog.accept(); else await dialog.dismiss();
    });
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== 'http://board.test') {
        unexpected.push(request.url());
        return route.abort();
      }
      if (request.method() === 'DELETE') {
        deletes.push({ pathname: url.pathname, body: request.postData() });
        pending = route;
        return;
      }
      if (request.method() === 'GET' && url.pathname === '/api/board/tasks/next-id') return route.fulfill({ json: { task_id: 'TEST-NEW' } });
      if (request.method() === 'GET' && url.pathname === '/api/board/tasks') return route.fulfill({ json: { tasks } });
      if (request.method() === 'GET' && url.pathname === '/api/archive/tasks/TEST-1') return route.fulfill({ json: task });
      if (request.isNavigationRequest()) return route.fulfill({ contentType: 'text/html', body: '<html><body></body></html>' });
      unexpected.push(`${request.method()} ${url.pathname}`);
      return route.abort();
    });

    async function open(status, standalone) {
      assert.equal(pending, undefined, 'Previous DELETE must be resolved');
      task = { task_id: 'TEST-1', title: 'Delete regression task', description: 'Description to preserve', status, attachments: [] };
      tasks = [task, ...statuses.map((value, index) => ({ task_id: `OTHER-${index}`, title: `${value} unaffected task`, description: 'Other task', status: value, attachments: [] }))];
      deletes.length = 0;
      confirmations.length = 0;
      accept = false;
      await page.goto(`http://board.test/${standalone ? 'tasks/TEST-1' : ''}`);
      let html = fs.readFileSync(path.join(root, 'templates', standalone ? 'task.html' : 'index.html'), 'utf8');
      const start = html.indexOf(standalone ? '<section class="task-page-editor"' : '<div class="modal-backdrop"');
      html = html.slice(start, html.indexOf('{% endblock %}', start));
      html = html.replace(/{% if task.status == "ARCHIVE" %}([\s\S]*?){% else %}([\s\S]*?){% endif %}/g,
        (_, archived, active) => status === 'ARCHIVE' ? archived : active).replace(/{{ task.task_id }}/g, 'TEST-1');
      const board = standalone ? '' : `<button class="button primary" id="new-task-button">New task</button><section class="board" id="board">${statuses.map(value => `<section class="board-column" data-status="${value}"><header class="column-header"><h2>${value}</h2><span class="column-count">0</span></header><div class="task-list" data-status="${value}"></div></section>`).join('')}</section>`;
      await page.setContent(board + html);
      for (const file of ['app', 'board', 'spinner', 'toast']) await page.addStyleTag({ path: path.join(root, `static/css/${file}.css`) });
      await page.evaluate(({ standalone, status }) => {
        if (standalone) {
          window.openTaskId = 'TEST-1';
          window.openTaskStatus = status;
        }
      }, { standalone, status });
      for (const file of ['toast', 'spinner', 'board']) await page.addScriptTag({ path: path.join(root, `static/js/${file}.js`) });
      await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
      if (!standalone) await page.locator('.task-card[data-task-id="TEST-1"]').click();
      await expect(page.locator('#task-title')).toHaveValue(task.title);
    }

    async function finish(response) {
      await expect.poll(() => Boolean(pending)).toBe(true);
      const route = pending;
      pending = undefined;
      await route.fulfill(response);
    }

    async function screenshot(name) {
      // Optional, explicitly requested visual artifacts; callers remove them after inspection.
      if (process.env.DELETE_TEST_SCREENSHOT_PREFIX) await page.screenshot({ path: `${process.env.DELETE_TEST_SCREENSHOT_PREFIX}-${name}.png`, fullPage: true });
    }

    for (const standalone of [false, true]) {
      for (const status of [...statuses, ...(standalone ? ['ARCHIVE'] : [])]) {
        await open(status, standalone);
        assert.equal(await page.locator('#delete-task').isVisible(), ['BACKLOG', 'ARCHIVE'].includes(status), `${status}, standalone=${standalone}`);
        if (!['BACKLOG', 'ARCHIVE'].includes(status)) {
          // Even selecting BACKLOG must not authorize deletion of a persisted different status.
          if (['IN PROGRESS', 'REVIEW'].includes(status)) await page.locator('#task-status').selectOption('BACKLOG');
          await expect(page.locator('#delete-task')).toBeHidden();
          await page.locator('#delete-task').dispatchEvent('click');
          assert.equal(confirmations.length, 0);
          assert.equal(deletes.length, 0);
        }
      }
      for (const status of standalone ? ['BACKLOG', 'ARCHIVE'] : ['BACKLOG']) {
        await open(status, standalone);
        const button = page.locator('#delete-task');
        const originalUrl = page.url();
        if (status === 'BACKLOG') {
          await page.locator('#task-title').fill('Unsaved title retained');
          await page.locator('#task-description').fill('Unsaved description retained');
          await page.locator('#task-status').selectOption('OPEN');
          await expect(button).toBeVisible();
        }
        await screenshot(`${standalone ? 'standalone' : 'modal'}-${status.toLowerCase()}`);
        // Cancel native confirmation: no request, navigation, spinner or lost form data.
        await button.click();
        assert.deepEqual(confirmations, [{ type: 'confirm', message: 'Delete this task and its files?' }]);
        assert.equal(deletes.length, 0);
        await expect(button).toBeEnabled();
        await expect(page.locator('#task-form')).not.toHaveAttribute('data-spinner-active', 'true');
        assert.equal(page.url(), originalUrl);
        await expect(page.locator('#task-modal')).toBeVisible();

        accept = true;
        await button.click();
        await expect.poll(() => deletes.length).toBe(1);
        await expect(page.locator('#task-form')).toHaveAttribute('data-spinner-active', 'true');
        await expect(page.locator('#task-form')).toHaveAttribute('aria-busy', 'true');
        await expect(button).toBeDisabled();
        assert.equal(await page.locator('#task-form input:enabled, #task-form select:enabled, #task-form textarea:enabled, #task-form button:enabled').count(), 0);
        await button.dispatchEvent('click');
        assert.equal(confirmations.length, 2, 'Duplicate clicks must not confirm again');
        assert.deepEqual(deletes, [{ pathname: `/api/${status === 'ARCHIVE' ? 'archive' : 'board'}/tasks/TEST-1`, body: null }]);
        await finish({ status: 502, json: { detail: 'Remote deletion failed' } });
        await expect(page.getByRole('alert')).toHaveText('Remote deletion failed');
        await expect(button).toBeEnabled();
        await expect(button).toHaveText('Delete');
        await expect(page.locator('#task-form')).not.toHaveAttribute('data-spinner-active', 'true');
        await expect(page.locator('#task-modal')).toBeVisible();
        assert.equal(page.url(), originalUrl);
        await expect(page.locator('#task-title')).toHaveValue(status === 'BACKLOG' ? 'Unsaved title retained' : task.title);
        await expect(page.locator('#task-description')).toHaveValue(status === 'BACKLOG' ? 'Unsaved description retained' : task.description);
        await expect(page.locator('#task-status')).toHaveValue(status === 'BACKLOG' ? 'OPEN' : 'ARCHIVE');
        if (status === 'ARCHIVE') await expect(page.locator('#task-title')).toBeDisabled();
        else await expect(page.locator('#task-title')).toBeEnabled();
        if (!standalone) await expect(page.locator('.task-card[data-task-id="TEST-1"]')).toHaveCount(1);

        // Retry succeeds with the API's empty 204 response, not JSON.
        await button.click();
        await expect.poll(() => deletes.length).toBe(2);
        await finish({ status: 204 });
        if (standalone) {
          await expect(page).toHaveURL(`http://board.test/${status === 'ARCHIVE' ? 'archive' : ''}`);
        } else {
          await expect(page.locator('#task-modal')).toBeHidden();
          await expect(page.locator('.task-card[data-task-id="TEST-1"]')).toHaveCount(0);
          await expect(page.locator('.task-card')).toHaveCount(6);
          await expect(page.locator('.board-column[data-status="BACKLOG"] .column-count')).toHaveText('1');
          await expect(page.getByRole('status')).toHaveText('Task deleted');
          await screenshot('board-after-delete');
        }
        assert.equal(deletes.length, 2);
      }
    }
    await open('BACKLOG', false);
    await page.locator('#cancel-task').click();
    await page.locator('#new-task-button').click();
    await expect(page.locator('#task-mode')).toHaveValue('create');
    await expect(page.locator('#delete-task')).toBeHidden();
    await page.locator('#delete-task').dispatchEvent('click');
    assert.equal(confirmations.length, 0);
    assert.equal(deletes.length, 0);
    assert.deepEqual(unexpected, [], 'No unexpected network requests or saves');
    assert.deepEqual(errors, [], 'No browser errors');
    console.log('PASS: Delete visibility and persisted-status guards, creation hidden, confirmation cancel, 502 Toast/form preservation, spinner and duplicate prevention, 204 modal refresh and standalone Board/Archive redirects; isolated routes only.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
