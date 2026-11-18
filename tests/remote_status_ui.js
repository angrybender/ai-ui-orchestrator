const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const server = spawn(process.env.PYTHON || 'python3', ['-u', 'tests/board_live_server.py'], {
    cwd: root, stdio: ['pipe', 'pipe', 'pipe']
  });
  let log = '';
  server.stderr.on('data', data => { log += data; });
  let browser;
  try {
    const info = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error(`Fixture timeout: ${log}`)), 15000);
      readline.createInterface({ input: server.stdout }).once('line', line => {
        clearTimeout(timeout); resolve(JSON.parse(line));
      });
      server.once('error', reject);
    });
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1459, height: 850 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(info.url);
    const status = page.locator('#remote-status');
    await expect(status).toHaveAttribute('data-state', 'offline');
    await expect(status.locator('.remote-status-limits')).toHaveText('');
    assert.equal(await status.locator('.remote-status-dot').evaluate(node => getComputedStyle(node).backgroundColor), 'rgb(241, 91, 91)');
    let data = { reachable: true, agent: 'codex', limits: [{ name: 'weekly', used_percent: 3, resets_at: '2028-11-21T08:14:35Z' }] };
    let requests = 0;
    await page.route('**/api/agent/status', route => {
      requests++;
      return route.fulfill({ json: data });
    });
    await page.clock.install();
    await page.reload();
    await expect(status).toHaveAttribute('data-state', 'online');
    await expect(status).toContainText('Codex limits remaining (reset at, UTC): weekly 97% (2028-11-21 08:14:35)');
    assert.equal(await status.locator('.remote-status-dot').evaluate(node => getComputedStyle(node).backgroundColor), 'rgb(67, 137, 231)');
    if (process.env.REMOTE_STATUS_SCREENSHOTS) await page.screenshot({ path: path.join(root, 'remote-status-desktop.png'), fullPage: true });
    data = { reachable: true, agent: 'claude', limits: [
      { name: 'weekly', used_percent: 30, resets_at: '2028-11-21T08:14:35Z' },
      { name: '5-hours', used_percent: 0, resets_at: '2028-11-16T15:00:00Z' }
    ] };
    const before = requests;
    await page.clock.fastForward(59000);
    assert.equal(requests, before);
    await page.clock.fastForward(1000);
    await expect(status).toContainText('Claude limits remaining (reset at, UTC): weekly 70% (2028-11-21 08:14:35)');
    await expect(status).toContainText('5-hours 100% (2028-11-16 15:00:00)');
    assert.equal(requests, before + 1);
    for (const width of [820, 375, 320]) {
      await page.setViewportSize({ width, height: 850 });
      assert(await page.locator('.board-heading').evaluate(node => node.scrollWidth <= node.clientWidth));
      for (const id of ['clear-db-button', 'agent-recovery-button', 'new-task-button']) {
        await expect(page.locator('#' + id)).toBeVisible();
        const box = await page.locator('#' + id).boundingBox();
        assert(box.x >= 0 && box.x + box.width <= width);
      }
    }
    if (process.env.REMOTE_STATUS_SCREENSHOTS) await page.screenshot({ path: path.join(root, 'remote-status-mobile.png'), fullPage: true });
    data.limits[0].used_percent = 100;
    data.limits[1].used_percent = 99.9;
    await page.clock.fastForward(60000);
    await expect(status).toContainText('weekly 0% (2028-11-21 08:14:35)');
    await expect(status).toContainText('5-hours 0.1% (2028-11-16 15:00:00)');
    data = { reachable: true, agent: 'claude', limits: [] };
    await page.clock.fastForward(60000);
    await expect(status.locator('.remote-status-limits')).toHaveText('');
    await expect(status).toHaveAttribute('data-state', 'online');
    data = { reachable: false, agent: 'claude', limits: [{ name: 'weekly', used_percent: 99, resets_at: '2028-11-21T08:14:35Z' }] };
    await page.clock.fastForward(60000);
    await expect(status).toHaveAttribute('data-state', 'offline');
    await expect(status.locator('.remote-status-limits')).toHaveText('');
    await page.route('**/api/agent/status', route => route.abort());
    await page.clock.fastForward(60000);
    await expect(status).toHaveAttribute('data-state', 'unknown');
    await expect(status.locator('.remote-status-limits')).toHaveText('');
    await expect(page.locator('.toast')).toHaveCount(0);
    await page.goto(`${info.url}/archive`);
    await expect(page.locator('#remote-status')).toHaveCount(0);
    assert.deepEqual(errors, []);
    console.log('Remote status: remaining quotas for Codex/Claude, 0%/100%/fractional remaining, SSH states, minute refresh, absent metadata, desktop/mobile, Board-only: passed');
  } finally {
    if (browser) await browser.close();
    server.stdin.end('\n');
    await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once('exit', resolve); });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
