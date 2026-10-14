const { chromium } = require('playwright');
const { expect } = require('playwright/test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

const root = path.resolve(__dirname, '..');
const fixture = path.join(__dirname, 'board_live_server.py');
const python = process.env.PYTHON || 'python3';

(async () => {
  const server = spawn(python, ['-u', fixture], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
  let serverLog = '';
  server.stderr.on('data', data => { serverLog += data; });
  let browser;
  let info;
  try {
    info = await new Promise((resolve, reject) => {
      const lines = readline.createInterface({ input: server.stdout });
      const timer = setTimeout(() => reject(new Error(`Fixture startup timed out: ${serverLog}`)), 15000);
      lines.once('line', line => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${serverLog}`)); });
    });
    assert.notEqual(path.resolve(info.directory), path.join(root, 'data'));
    browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1568, height: 1050 } });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await expect.poll(async () => {
      try { return (await context.request.get(`${info.url}/health`)).status(); } catch (_) { return 0; }
    }, { timeout: 15000 }).toBe(200);
    await page.goto(info.url, { waitUntil: 'domcontentloaded' });
    const inputs = page.locator('.task-file-input');
    const file = name => ({ name, mimeType: 'text/plain', buffer: Buffer.from(name) });
    const checkRows = async count => {
      await expect(inputs).toHaveCount(count);
      await expect(page.locator('#add-file-upload')).toHaveCount(0);
      assert.equal(await inputs.evaluateAll(nodes => nodes.filter(n => !n.files.length).length), 1);
      assert.equal(await inputs.last().evaluate(n => n.files.length), 0);
      const uploadBox = await inputs.last().boundingBox();
      const fileBoxes = await page.locator('.attachment-row, .file-upload-selected').evaluateAll(rows => rows.map(row => row.getBoundingClientRect().bottom));
      assert.ok(fileBoxes.every(bottom => bottom <= uploadBox.y), 'Upload button follows all saved and selected files');
      await expect(page.locator('.file-upload-remove:visible')).toHaveCount(count - 1);
    };
    await page.locator('#new-task-button').click();
    await checkRows(1);
    await inputs.first().setInputFiles(file('first.txt'));
    await expect(inputs.first()).toBeHidden();
    await expect(page.locator('.file-upload-name:visible')).toHaveText('first.txt');
    await checkRows(2);
    await inputs.first().setInputFiles(file('replacement.txt'));
    await checkRows(2);
    await inputs.last().setInputFiles(file('second.txt'));
    await checkRows(3);
    await inputs.first().setInputFiles([]);
    await checkRows(2);
    await page.locator('.file-upload-remove:visible').click();
    await checkRows(1);
    await inputs.first().setInputFiles(file('saved.txt'));
    await inputs.last().setInputFiles(file('also-saved.txt'));
    await checkRows(3);
    await page.locator('#task-title').fill('Automatic file uploads');
    await page.locator('#task-description').fill('Upload regression');
    const savedId = await page.locator('#task-id').inputValue();
    if (process.env.FILE_UPLOAD_SCREENSHOTS === '1') await page.screenshot({ path: '/app/file-upload-desktop.png' });
    await page.locator('#save-task').click();
    await expect(page.locator('#task-modal')).toBeHidden();
    await page.locator(`.task-card[data-task-id="${savedId}"]`).click();
    await checkRows(1);
    await expect(page.locator('.attachment-row')).toHaveCount(2);
    const savedStyle = await page.locator('.attachment-row').first().evaluate(row => {
      const style = getComputedStyle(row);
      const button = getComputedStyle(row.querySelector('button'));
      return [style.backgroundColor, style.padding, style.borderRadius, style.fontSize, button.backgroundColor, button.padding, button.fontSize];
    });
    await inputs.first().setInputFiles(file('third.txt'));
    await checkRows(2);
    const selectedStyle = await page.locator('.file-upload-selected').evaluate(row => {
      const style = getComputedStyle(row);
      const button = getComputedStyle(row.querySelector('button'));
      return [style.backgroundColor, style.padding, style.borderRadius, style.fontSize, button.backgroundColor, button.padding, button.fontSize];
    });
    assert.deepEqual(selectedStyle, savedStyle, 'Selected and saved attachment styles match');
    await page.locator('#cancel-task').click();
    await page.locator(`.task-card[data-task-id="${savedId}"]`).click();
    await checkRows(1);
    await page.goto(`${info.url}/tasks/${savedId}`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#task-title')).toHaveValue('Automatic file uploads');
    await checkRows(1);
    await inputs.first().setInputFiles(file('third.txt'));
    await checkRows(2);
    await page.locator('#save-task').click();
    await expect(page.locator('.attachment-row')).toHaveCount(3);
    await checkRows(1);
    await page.setViewportSize({ width: 375, height: 900 });
    await inputs.first().setInputFiles(file('mobile.txt'));
    await checkRows(2);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const removeBox = await page.locator('.file-upload-remove:visible').boundingBox();
    assert.ok(removeBox.x + removeBox.width <= 375, 'Remove stays within mobile viewport');
    if (process.env.FILE_UPLOAD_SCREENSHOTS === '1') await page.screenshot({ path: '/app/file-upload-mobile.png', fullPage: true });
    assert.deepEqual(errors, []);
    console.log('PASS: automatic upload rows, replacement, clearing, removal, persistence, reopen, standalone edit, mobile, browser errors.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && server.signalCode === null && server.pid) {
      const exited = new Promise(resolve => server.once('exit', resolve));
      server.stdin.end('\n');
      const timeout = setTimeout(() => server.kill('SIGKILL'), 10000);
      await exited;
      clearTimeout(timeout);
    }
    if (info) assert.equal(fs.existsSync(info.directory), false, 'Fixture must clean its temporary database');
    if (serverLog.includes('Traceback')) console.error(serverLog);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
