const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const html = execFileSync('python3', ['-c', `
import json
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
root = Path.cwd()
env = Environment(loader=FileSystemLoader(root / 'templates'), autoescape=True)
section = json.loads((root / 'settings/ui/agent.json').read_text())
section['key'] = 'agent'
section['groups'].append({'title': 'Regression fields', 'fields': [
    {'id': 'short', 'type': 'string', 'required': True, 'description': 'Description below the field'},
    {'id': 'password', 'type': 'password', 'required': True},
    {'id': 'long_text', 'type': 'text', 'sub_type': 'big', 'required': True},
]})
values = {f['id']: f.get('default', 'Sample value') for g in section['groups'] for f in g['fields']}
print(env.get_template('settings.html').render(selected=section, sections=[section], values=values, url_for=lambda *args, **kwargs: '/static/' + kwargs.get('path', '')))
`], { cwd: root, encoding: 'utf8' });
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '').replace(/<link\b[^>]*>/g, ''));
    await page.addStyleTag({ path: path.join(root, 'static/css/app.css') });
    for (const width of [1440, 1073, 768, 560, 375, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const mark of await page.locator('.required-mark').all()) {
        const geometry = await mark.evaluate(node => {
          const field = node.previousElementSibling;
          const box = field.getBoundingClientRect();
          const star = node.getBoundingClientRect();
          return { gap: star.left - box.right, top: star.top - box.top, right: star.right, required: field.required };
        });
        assert.ok(Math.abs(geometry.gap - 8) < 1, `Star must be outside field: ${width} ${JSON.stringify(geometry)}`);
        assert.equal(geometry.top, 0);
        assert.ok(geometry.right <= width);
        assert.ok(geometry.required);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.SETTINGS_TEST_SCREENSHOT_PREFIX && [1073, 375].includes(width)) {
        await page.screenshot({ path: `${process.env.SETTINGS_TEST_SCREENSHOT_PREFIX}-${width}.png`, fullPage: true });
      }
    }
    await page.locator('#prompt').fill('');
    assert.equal(await page.locator('#prompt').evaluate(node => node.checkValidity()), false);
    console.log('PASS: required markers outside all 6 field variants at 6 viewport widths; required validation preserved');
    for (const template of ['index.html', 'task.html']) {
      const source = require('node:fs').readFileSync(path.join(root, 'templates', template), 'utf8');
      const start = source.indexOf('<form id="task-form"');
      const form = source.slice(start, source.indexOf('</form>', start) + 7);
      await page.setContent(`<div class="task-dialog">${form}</div>`);
      for (const name of ['app', 'board']) await page.addStyleTag({ path: path.join(root, `static/css/${name}.css`) });
      for (const width of [1073, 768, 375]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const id of ['task-id', 'task-title', 'task-description']) {
          const geometry = await page.locator(`#${id}`).evaluate(field => {
            const box = field.getBoundingClientRect();
            const wrapper = field.parentElement.getBoundingClientRect();
            const label = field.previousElementSibling.getBoundingClientRect();
            const error = field.nextElementSibling.getBoundingClientRect();
            return { width: box.width, expected: wrapper.width, top: box.top, labelBottom: label.bottom, bottom: box.bottom, errorTop: error.top };
          });
          assert.ok(Math.abs(geometry.width - geometry.expected) < 1, `${template} ${width} ${id}: full-width field`);
          assert.ok(geometry.top >= geometry.labelBottom, 'Label must be above field');
          assert.ok(geometry.errorTop >= geometry.bottom, 'Error must be below field');
        }
        assert.equal(await page.locator('#status-control').isVisible(), template === 'task.html');
        if (process.env.SETTINGS_TEST_SCREENSHOT_PREFIX && template === 'index.html') {
          await page.screenshot({ path: `${process.env.SETTINGS_TEST_SCREENSHOT_PREFIX}-task-${width}.png`, fullPage: true });
        }
      }
    }
    console.log('PASS: create/edit task fields retain full width, stacked labels/errors and hidden status at 3 viewport widths');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
