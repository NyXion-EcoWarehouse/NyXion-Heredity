import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import readline from 'node:readline';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(import.meta.url);
const playwright = require(process.env.PLAYWRIGHT_PACKAGE ?? 'playwright');
const python = process.env.NYXION_TEST_PYTHON
    ?? path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const output = path.join(path.resolve(process.env.HEREDITY_BROWSER_OUTPUT ?? path.join(root, '.heredity')),
    'ui-settings-attention-qa');
await fs.mkdir(output, { recursive: true });
const child = spawn(python, [
    path.join(root, 'webui/tests/settings-attention-browser.fixture.py'),
    '--state-dir', path.join(output, `fixture-${Date.now()}`),
], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
const stderr = [], replies = new Map();
child.stderr.on('data', (chunk) => stderr.push(chunk.toString()));
const lines = readline.createInterface({ input: child.stdout });
let browser;
const command = (operation) => new Promise((resolve, reject) => {
    const command_id = crypto.randomUUID();
    const timer = setTimeout(() => reject(new Error(`Fixture command timed out: ${operation}`)), 8000);
    replies.set(command_id, (result) => { clearTimeout(timer); resolve(result); });
    child.stdin.write(JSON.stringify({ command_id, operation }) + '\n');
});
async function bounds(page) {
    const value = await page.evaluate(() => {
        const main = document.querySelector('.page-scroll').getBoundingClientRect();
        const header = document.querySelector('.topbar').getBoundingClientRect();
        const footer = document.querySelector('.workspace-footer').getBoundingClientRect();
        return { mainTop: main.top, mainBottom: main.bottom, headerTop: header.top,
            headerBottom: header.bottom, footerTop: footer.top, footerBottom: footer.bottom,
            height: innerHeight, width: innerWidth, scrollWidth: document.documentElement.scrollWidth };
    });
    assert.equal(value.headerTop, 0);
    assert.equal(value.mainTop, value.headerBottom);
    assert.equal(value.mainBottom, value.footerTop);
    assert.equal(value.footerBottom, value.height);
    assert.equal(value.scrollWidth, value.width);
}

try {
    const ready = await Promise.race([
        new Promise((resolve, reject) => {
            lines.on('line', (line) => {
                try {
                    const value = JSON.parse(line);
                    if (value.ui) resolve(value);
                    if (replies.has(value.command_id)) {
                        replies.get(value.command_id)(value.result);
                        replies.delete(value.command_id);
                    }
                } catch { /* Ignore non-structured fixture output. */ }
            });
            child.on('exit', (code) => reject(new Error(`Fixture exited ${code}: ${stderr.join('')}`)));
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('Fixture startup timed out')), 12000)),
    ]);
    const origin = `http://127.0.0.1:${ready.ui[1]}`;
    browser = await playwright.chromium.launch({
        headless: true,
        ...(process.env.NYXION_TEST_BROWSER ? { executablePath: process.env.NYXION_TEST_BROWSER } : {}),
    });
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 }, colorScheme: 'light' });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [], mutations = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        if (request.method() !== 'GET') mutations.push({
            path: new URL(request.url()).pathname, method: request.method(), body: request.postData(),
        });
    });
    await page.goto(origin + '/settings');
    await page.getByLabel('访问密码').fill('settings-browser-fixture-2026');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('heading', { name: '配置', exact: true }).waitFor();
    const nav = page.getByRole('navigation', { name: '主导航' });
    await nav.getByRole('button', { name: '离线诊断', exact: true }).waitFor();
    await nav.getByRole('button', { name: '离线诊断', exact: true }).locator('.nav-attention-error').waitFor();
    await nav.getByRole('button', { name: '网络接口', exact: true }).locator('.nav-attention-error').waitFor();
    await nav.getByRole('button', { name: '模块商场', exact: true }).locator('.nav-attention-warning').waitFor();
    assert.equal(await nav.getByRole('button', { name: '配置', exact: true }).locator('.nav-attention').count(), 0);
    const management = (route) => page.evaluate(async (route) => {
        const result = await fetch(route, { headers: { 'X-Heredity-Token': localStorage.getItem('heredity_token') } });
        return result.json();
    }, route);
    const before = await management('/api/heredity/settings');
    assert.equal(before.startup.ui_port, ready.ui[1]);
    assert.equal(before.startup.configured_ui_port, ready.ui[1]);
    assert.equal(before.startup.restart_required, false);
    assert.equal(before.services.length, 3);
    await page.getByRole('row').filter({ hasText: '独立 WS 服务' }).getByText('9999', { exact: true }).waitFor();
    const portInput = page.getByLabel('WebUI 端口', { exact: false });
    const targetPort = ready.ui[1] === 2000 ? 9999 : 2000;
    await portInput.fill(String(targetPort));
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith('/api/heredity/settings') && response.status() === 200),
        page.getByRole('button', { name: '刷新', exact: true }).click(),
    ]);
    assert.equal(await portInput.inputValue(), String(targetPort));
    await page.getByRole('button', { name: '保存端口', exact: true }).click();
    await page.getByText('WebUI 端口已保存到 .env，下次宿主重启后生效。', { exact: true }).waitFor();
    const after = await management('/api/heredity/settings');
    assert.equal(after.startup.ui_port, ready.ui[1]);
    assert.equal(after.startup.configured_ui_port, targetPort);
    assert.equal(after.startup.restart_required, true);
    assert.deepEqual(after.services, before.services);
    const saved = await command('read-env');
    assert.ok(saved.text.includes(`HEREDITY_UI_PORT=${targetPort}`));
    assert.ok(saved.text.includes('OTHER_VALUE=preserved'));
    const settingsMutation = mutations.find((item) => item.path === '/api/heredity/settings');
    assert.equal(settingsMutation.method, 'PUT');
    assert.deepEqual(JSON.parse(settingsMutation.body), { ui_port: targetPort });
    await nav.getByRole('button', { name: '配置', exact: true }).locator('.nav-attention-warning').waitFor();
    await page.locator('[data-attention-severity="warning"]')
        .getByText('启动端口已修改，下次启动生效。', { exact: true }).waitFor();
    assert.ok(await nav.getByRole('button', { name: '网络接口', exact: true }).getAttribute('title'));
    for (const scheme of ['light', 'dark']) {
        await page.emulateMedia({ colorScheme: scheme });
        await bounds(page);
        await page.locator('.page-scroll').evaluate((element) => element.scrollTo(0, 0));
        await page.screenshot({ path: path.join(output, `settings-desktop-${scheme}.png`), animations: 'disabled' });
    }
    await page.getByRole('button', { name: '网络接口', exact: true }).last().click();
    await page.getByRole('heading', { name: '网络接口', exact: true }).waitFor();
    await page.locator('[data-attention-severity="error"]').waitFor();
    assert.equal(await page.getByRole('button', { name: '拒绝', exact: true }).count(), 0);
    await nav.getByRole('button', { name: '离线诊断', exact: true }).click();
    await page.getByRole('heading', { name: '离线诊断', exact: true }).waitFor();
    await page.locator('[data-attention-severity="error"]')
        .getByText('插件或账号会话出现异常，请检查对应页面。', { exact: true }).waitFor();
    const labels = await nav.locator('button > span:first-of-type').allTextContents();
    assert.ok(labels.indexOf('离线诊断') < labels.indexOf('插件'));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '打开导航', exact: true }).click();
    await nav.getByRole('button', { name: '配置', exact: true }).locator('.nav-attention-warning').waitFor();
    await page.screenshot({ path: path.join(output, 'attention-mobile-sidebar.png'), animations: 'disabled' });
    await nav.getByRole('button', { name: '配置', exact: true }).click();
    await page.getByRole('heading', { name: '配置', exact: true }).waitFor();
    for (const scheme of ['light', 'dark']) {
        await page.emulateMedia({ colorScheme: scheme });
        await bounds(page);
        await page.locator('.page-scroll').evaluate((element) => element.scrollTo(0, 0));
        await page.screenshot({ path: path.join(output, `settings-mobile-${scheme}.png`), animations: 'disabled' });
    }
    assert.ok(!mutations.some((item) => item.path.includes('/plugins/')
        || item.path.includes('/market/install') || item.path === '/api/debug/invoke'));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({
        scope: 'isolated dotenv settings and real host attention summaries',
        configured_port_saved: true, current_listener_unchanged: true, unrelated_env_preserved: true,
        independent_services_reported: 3, sidebar_error_warning_badges: true,
        plugin_registered_path_attention: true, current_page_notice: true,
        desktop: '1365x1000', mobile: '390x844', fixed_chrome_preserved: true,
        page_errors: errors, message_send_requests: 0, output,
    }, null, 2));
} finally {
    if (browser) await browser.close();
    if (child.exitCode === null) {
        child.stdin.end('\n');
        await Promise.race([once(child, 'exit'), new Promise((resolve) => setTimeout(resolve, 10000))]);
        if (child.exitCode === null) { child.kill(); await once(child, 'exit'); }
    }
}
