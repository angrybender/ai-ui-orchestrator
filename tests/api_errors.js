const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

(async () => {
  const root = path.resolve(__dirname, '..');
  const pages = JSON.parse(execFileSync(path.join(root, 'dev-venv/bin/python'), ['-c', `
import json
from jinja2 import Environment, FileSystemLoader
from settings.settings import load_sections
sections = load_sections()
section = next(s for s in sections if s['key'] == 'board')
values = {f['id']: f.get('default', '') for g in section['groups'] for f in g['fields']}
env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
base = dict(url_for=lambda *args, **kw: '/static/' + kw.get('path', ''))
print(json.dumps({
 'settings': env.get_template('settings.html').render(**base, sections=sections, selected=section, values=values, active='settings'),
 'archive': env.get_template('archive.html').render(**base, active='archive'),
 'board': env.get_template('index.html').render(**base, active='board'),
 'proxy': env.get_template('settings.html').render(**base, sections=sections, selected={'key':'http_proxy'}, values={'proxies':[]}, active='settings')
}))
`], { cwd: root, encoding: 'utf8' }));
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 950 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let current = 'settings';
    let chatTask = null;
    let reply = { status: 500, json: { detail: 'Database unavailable' } };
    await page.route('http://errors.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/static/')) return route.fulfill({ path: path.join(root, url.pathname) });
      if (url.pathname.startsWith('/api/')) {
        if (url.pathname.endsWith('/next-id')) return route.fulfill({ json: { task_id: 'ERR-1' } });
        if (url.pathname === '/api/board/tasks' && route.request().method() === 'GET') return route.fulfill({ json: { tasks: chatTask ? [chatTask] : [] } });
        if (chatTask && url.pathname.endsWith('/chat') && route.request().method() === 'GET') return route.fulfill({ json: { status: 'REVIEW', messages: [] } });
        if (reply === 'network') return route.abort('connectionrefused');
        return route.fulfill(reply);
      }
      return route.fulfill({ contentType: 'text/html', body: pages[current] });
    });
    // SSE reconnection has its own existing error UI; no real server is used here.
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    async function open(name) { current = name; await page.goto('http://errors.test/'); }
    async function failure(expected, action) {
      await page.locator('.toast').evaluateAll(nodes => nodes.forEach(node => node.remove()));
      await action();
      await page.waitForFunction(text => document.querySelector('.toast[role="alert"]')?.textContent === text, expected);
      assert.equal(await page.locator('.toast[role="alert"]').count(), 1, 'one toast per failure');
    }
    await open('settings');
    for (const status of [400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504, 599]) {
      reply = { status, json: { detail: `Backend error ${status}` } };
      await failure(`Backend error ${status}`, () => page.evaluate(async () => { try { await fetch('/api/check', {method: 'POST'}); } catch (error) {} }));
    }
    const formats = [
      [{ detail: { title: 'Invalid title', nested: ['First', 'Second'] } }, 'title: Invalid title; nested: First; Second'],
      [{ detail: [{ loc: ['body', 'values', 'port'], msg: 'Invalid integer', type: 'value_error' }] }, 'values.port: Invalid integer'],
      [{ message: 'Message error' }, 'Message error'],
      [{ error: 'Error field' }, 'Error field'],
      [{}, 'Request failed (HTTP 500 Internal Server Error)'],
    ];
    for (const [json, expected] of formats) {
      reply = { status: 500, json };
      await failure(expected, () => page.evaluate(async () => { try { await fetch('/api/check', {method: 'POST'}); } catch (error) {} }));
    }
    for (const body of ['', '<html><body>Proxy failure</body></html>', 'Traceback (most recent call last): internal details']) {
      reply = { status: 500, body };
      await failure('Request failed (HTTP 500 Internal Server Error)', () => page.evaluate(async () => { try { await fetch('/api/check', {method: 'POST'}); } catch (error) {} }));
    }
    reply = { status: 502, body: 'Upstream unavailable' };
    await failure('Upstream unavailable', () => page.getByRole('button', {name: 'Save', exact: true}).click());
    assert.equal(await page.getByRole('button', {name: 'Save', exact: true}).isEnabled(), true);
    reply = { status: 422, json: { detail: [{loc: ['body', 'prefix'], msg: 'Invalid prefix'}] } };
    await failure('prefix: Invalid prefix', () => page.getByRole('button', {name: 'Save', exact: true}).click());
    if (process.env.API_ERRORS_SCREENSHOT_PREFIX) { await page.waitForFunction(() => getComputedStyle(document.querySelector('.toast')).opacity === '1'); await page.screenshot({path: `${process.env.API_ERRORS_SCREENSHOT_PREFIX}-settings.png`, fullPage: true}); }
    reply = 'network';
    await failure('Unable to connect to the server. Please try again.', () => page.getByRole('button', {name: 'Save', exact: true}).click());
    reply = { status: 503, json: { detail: 'Polling unavailable' } };
    await failure('Polling unavailable', () => page.evaluate(async () => { for (let i=0; i<3; i++) { try { await fetch('/api/board/tasks/TEST-1/chat'); } catch (error) {} } }));
    reply = { json: { ok: true } };
    assert.equal(await page.evaluate(async () => (await fetch('/api/board/tasks/TEST-1/chat')).ok), true);
    reply = { status: 503, json: { detail: 'Polling unavailable' } };
    await failure('Polling unavailable', () => page.evaluate(async () => { try { await fetch('/api/board/tasks/TEST-1/chat'); } catch (error) {} }));
    await page.locator('.toast').evaluateAll(nodes => nodes.forEach(node => node.remove()));
    await page.evaluate(async () => { const controller = new AbortController(); controller.abort(); try { await fetch('/api/board/tasks/TEST-1/chat', {signal: controller.signal}); } catch (error) {} });
    assert.equal(await page.locator('.toast').count(), 0, 'intentional abort is silent');
    reply = { status: 503, json: { detail: 'Archive database unavailable' } };
    await open('archive');
    await page.waitForSelector('.toast[role="alert"]');
    assert.equal(await page.locator('.toast[role="alert"]').textContent(), 'Archive database unavailable');
    await open('board');
    reply = { status: 422, json: { detail: { title: 'Invalid task title' } } };
    await page.locator('#new-task-button').click();
    await page.locator('#task-title').fill('Keep this title');
    await page.locator('#task-description').fill('Keep this description');
    await failure('title: Invalid task title', () => page.locator('#save-task').click());
    assert.equal(await page.locator('[data-error="title"]').textContent(), 'Invalid task title');
    assert.equal(await page.locator('#task-title').inputValue(), 'Keep this title');
    assert.equal(await page.locator('#save-task').isEnabled(), true);
    for (const width of [1280, 375, 320]) {
      await page.setViewportSize({width, height: 950});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const box = await page.locator('.toast[role="alert"]').boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width);
      if (process.env.API_ERRORS_SCREENSHOT_PREFIX && width !== 320) { await page.waitForFunction(() => getComputedStyle(document.querySelector('.toast')).opacity === '1'); await page.screenshot({path: `${process.env.API_ERRORS_SCREENSHOT_PREFIX}-board-${width}.png`, fullPage: true}); }
    }
    await page.locator('#cancel-task').click();
    reply = { status: 503, json: { detail: 'Recovery unavailable' } };
    for (let i = 0; i < 2; i++) {
      await failure('Recovery unavailable', () => page.locator('#agent-recovery-button').click());
      assert.equal(await page.locator('#agent-recovery-button').isEnabled(), true);
    }
    chatTask = {task_id: 'CHAT-1', title: 'Chat task', description: 'Description', status: 'REVIEW', attachments: []};
    await open('board');
    await page.locator('.task-card').click();
    await page.locator('#chat-comment').fill('Keep this comment');
    reply = { status: 422, json: { detail: { comment: 'Comment rejected' } } };
    await failure('comment: Comment rejected', () => page.locator('#send-comment').click());
    assert.equal(await page.locator('#chat-error').textContent(), 'Comment rejected');
    assert.equal(await page.locator('#chat-comment').inputValue(), 'Keep this comment');
    assert.equal(await page.locator('#send-comment').isEnabled(), true);
    reply = { status: 500, body: '<html>Internal error</html>' };
    await failure('Request failed (HTTP 500 Internal Server Error)', () => page.locator('#send-comment').click());
    assert.equal(await page.locator('#chat-comment').inputValue(), 'Keep this comment');
    await open('proxy');
    reply = { status: 500, body: '<html>Error</html>' };
    await failure('Request failed (HTTP 500 Internal Server Error)', () => page.getByRole('button', {name: 'Save', exact: true}).click());
    assert.equal(await page.getByRole('button', {name: 'Save', exact: true}).isEnabled(), true);
    assert.deepEqual(errors, []);
    console.log('PASS: HTTP errors, structured validation, non-JSON/empty responses, network failures, polling recovery, abort, Settings/Archive/Board/HTTP proxy integration and responsive toast');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
