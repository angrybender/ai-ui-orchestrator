const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const html = execFileSync(path.join(root, 'dev-venv/bin/python'), ['-c', `
from jinja2 import Environment, FileSystemLoader
from settings.settings import load_sections
section = {'key': 'http_proxy', 'title': 'HTTP proxy'}
env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
print(env.get_template('settings.html').render(selected=section, sections=load_sections(), values={'proxies': []}, proxy_errors=['Proxy 2 (port 8002): could not start.'], active='settings', url_for=lambda *args, **kwargs: '/static/' + kwargs.get('path', '')))
`], {cwd: root, encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 950}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '').replace(/<link\b[^>]*>/g, ''));
    for (const name of ['app', 'http-proxy', 'spinner', 'toast']) await page.addStyleTag({path: path.join(root, `static/css/${name}.css`)});
    for (const name of ['spinner', 'toast', 'http-proxy']) await page.addScriptTag({path: path.join(root, `static/js/${name}.js`)});
    assert.equal(await page.locator('.settings-tab').last().textContent(), 'HTTP proxy');
    assert.equal(await page.locator('.proxy-block').count(), 0);
    await page.getByRole('button', {name: 'Add proxy', exact: true}).click();
    assert.equal(await page.locator('[name=timeout]').inputValue(), '30');
    assert.equal(await page.locator('[name=method]').inputValue(), 'GET');
    assert.equal(await page.getByRole('button', {name: 'Remove method'}).isDisabled(), true);
    assert.equal(await page.getByRole('button', {name: 'Remove pattern'}).isDisabled(), true);
    await page.getByRole('button', {name: 'Add method'}).click();
    assert.equal(await page.locator('[name=method]').last().inputValue(), 'GET');
    await page.getByRole('button', {name: 'Remove method'}).last().click();
    await page.getByRole('button', {name: 'Pattern operator'}).click();
    assert.equal(await page.getByRole('button', {name: 'Pattern operator'}).textContent(), '!');
    await page.getByRole('button', {name: 'Add pattern'}).click();
    assert.equal(await page.getByRole('button', {name: 'Pattern operator'}).last().textContent(), '=');
    await page.locator('[name=port]').fill('8001');
    await page.locator('[name=remote_url]').fill('https://example.test/');
    await page.locator('[name=method]').selectOption('ALL');
    await page.locator('[name=pattern]').first().fill(' ^/private');
    for (const width of [1440, 900, 560, 375, 320]) {
      await page.setViewportSize({width, height: 950});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `overflow at ${width}`);
      assert.ok(Math.abs((await page.locator('[name=port]').boundingBox()).y - (await page.locator('[name=timeout]').boundingBox()).y) < 1);
    }
    await page.setViewportSize({width: 1440, height: 950});
    if (process.env.HTTP_PROXY_SCREENSHOT) await page.screenshot({path: process.env.HTTP_PROXY_SCREENSHOT, fullPage: true});
    await page.evaluate(() => {
      window.requests = [];
      window.fetch = (url, options) => {
        window.requests.push(JSON.parse(options.body));
        return new Promise(resolve => { window.finishSave = (ok, restartRequired = false) => resolve({ok, json: async () => ({restart_required: restartRequired, detail: ['Proxy 1: remote URL is not reachable.']})}); });
      };
    });
    await page.getByRole('button', {name: 'Save', exact: true}).click();
    assert.equal(await page.locator('#http-proxy-form').getAttribute('aria-busy'), 'true');
    assert.equal(await page.locator('#http-proxy-form input:enabled, #http-proxy-form select:enabled, #http-proxy-form button:enabled').count(), 0);
    await page.evaluate(() => document.querySelector('#http-proxy-form').dispatchEvent(new Event('submit', {cancelable: true})));
    assert.equal(await page.evaluate(() => window.requests.length), 1);
    assert.deepEqual(await page.evaluate(() => window.requests[0].values.proxies[0].methods), ['ALL']);
    assert.deepEqual(await page.evaluate(() => window.requests[0].values.proxies[0].patterns), [{operator: '!', pattern: ' ^/private'}]);
    await page.evaluate(() => window.finishSave(false));
    await page.getByText('Proxy 1: remote URL is not reachable.', {exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: 'Save', exact: true}).isEnabled(), true);
    await page.getByRole('button', {name: 'Save', exact: true}).click();
    await page.evaluate(() => window.finishSave(true));
    await page.getByText('Saved. Methods and patterns applied without restarting.', {exact: true}).waitFor();
    await page.getByRole('button', {name: 'Remove proxy', exact: true}).click();
    await page.getByRole('button', {name: 'Save', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => window.requests.at(-1)), {values: {proxies: []}});
    await page.evaluate(() => window.finishSave(true, true));
    await page.getByText('Saved. Methods and patterns applied to running proxies. Restart to apply proxy additions, removals, port, remote URL or timeout changes.', {exact: true}).waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: HTTP proxy controls, defaults, rules, spinner, errors, live/restart Toasts, empty Save and layout at 5 widths');
    for (const methods of [[], ['ALL'], ['GET', 'POST']]) {
      await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '').replace(/<link\b[^>]*>/g, ''));
      await page.locator('#http-proxy-form').evaluate((form, value) => { form.dataset.proxies = JSON.stringify([{port: 8001, timeout: 30, remote_url: 'http://example.test/', methods: value, patterns: [{operator: '!', pattern: '^/one'}, {operator: '=', pattern: '^/two'}]}]); }, methods);
      await page.addScriptTag({path: path.join(root, 'static/js/http-proxy.js')});
      assert.deepEqual(await page.locator('[name=method]').evaluateAll(fields => fields.map(field => field.value)), methods.length ? methods : ['ALL']);
      assert.deepEqual(await page.locator('[name=pattern]').evaluateAll(fields => fields.map(field => field.value)), ['^/one', '^/two']);
    }
    console.log('PASS: persisted ALL/empty methods and every saved method/pattern restored');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
