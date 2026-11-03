const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');
const fs = require('node:fs');

(async () => {
  const root = path.resolve(__dirname, '..');
  const server = spawn(process.env.PYTHON || 'python3', ['-u', 'tests/board_live_server.py'], {
    cwd: root, stdio: ['pipe', 'pipe', 'pipe']
  });
  let browser, info, serverLog = '';
  server.stderr.on('data', data => { serverLog += data; });
  try {
    info = await new Promise((resolve, reject) => {
      const lines = readline.createInterface({ input: server.stdout });
      const timer = setTimeout(() => reject(new Error(serverLog || 'Fixture startup timed out')), 15000);
      lines.once('line', line => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${serverLog}`)); });
    });
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1357, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${info.url}/settings?section=agent`, { waitUntil: 'domcontentloaded' });
    const checkbox = page.getByRole('checkbox', { name: /ACP wait background tasks$/ });
    await expect(checkbox).not.toBeChecked();
    const quietTimeout = page.locator('#acp_background_quiet_timeout');
    await expect(quietTimeout).toHaveValue('60');
    await expect(quietTimeout).toHaveAttribute('type', 'number');
    await expect(quietTimeout).toHaveAttribute('step', '1');
    await expect(quietTimeout).toHaveAttribute('min', '1');
    assert.equal(await quietTimeout.evaluate(node =>
      node.closest('.field-control').previousElementSibling.contains(
        document.getElementById('acp_wait_background_tasks'))), true);
    for (const value of ['0', '-1', '1.5', '']) {
      await quietTimeout.fill(value);
      assert.equal(await quietTimeout.evaluate(node => node.checkValidity()), false);
    }
    await expect(page.locator('h1')).toHaveCount(0);
    await page.locator('#shell').fill('qwen --acp --yolo');
    await page.locator('#agent_timeout').fill('120');
    await page.locator('#prompt').fill('Complete and verify all task work.');
    for (const enabled of [true, false]) {
      await checkbox.setChecked(enabled);
      const timeout = enabled ? 75 : 60;
      await quietTimeout.fill(String(timeout));
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/settings/agent');
      await page.getByRole('button', { name: 'Save', exact: true }).click();
      const response = await saved;
      assert.equal(response.status(), 200);
      const values = (await response.json()).values;
      assert.equal(values.acp_wait_background_tasks, enabled);
      assert.equal(values.acp_background_quiet_timeout, timeout);
      await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
      await page.reload({ waitUntil: 'domcontentloaded' });
      assert.equal(await checkbox.isChecked(), enabled);
      await expect(quietTimeout).toHaveValue(String(timeout));
    }
    for (const value of [0, -1, 1.5, true]) {
      const response = await page.request.post(`${info.url}/api/settings/agent`, { data: { values: {
        shell: 'qwen --acp --yolo', agent_timeout: 120, prompt: 'Complete work.',
        acp_wait_background_tasks: false, acp_background_quiet_timeout: value
      } } });
      assert.equal(response.status(), 422);
    }
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(quietTimeout).toHaveValue('60');
    await checkbox.check();
    for (const width of [1357, 375, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await expect(checkbox).toBeChecked();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const label = await page.locator('label[for="acp_wait_background_tasks"]').boundingBox();
      assert.ok(label.x >= 0 && label.x + label.width <= width);
      if (process.env.ACP_WAIT_SCREENSHOTS && width !== 320) {
        await page.screenshot({ path: `${process.env.ACP_WAIT_SCREENSHOTS}-${width}.png`, fullPage: true });
      }
    }
    assert.deepEqual(errors, []);
    console.log('PASS: Agent checkbox and integer quiet timeout default/save/reload/validation pass; no overflow at 1357/375/320px.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && server.signalCode === null) {
      const exited = new Promise(resolve => server.once('exit', resolve));
      server.stdin.end('\n');
      const timer = setTimeout(() => server.kill('SIGKILL'), 10000);
      await exited;
      clearTimeout(timer);
    }
    if (info) assert.equal(fs.existsSync(info.directory), false);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
