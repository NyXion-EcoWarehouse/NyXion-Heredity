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
    'ui-market-qa');
await fs.mkdir(output, { recursive: true });
const child = spawn(python, [
    path.join(root, 'webui/tests/market-browser.fixture.py'),
    '--state-dir', path.join(output, `fixture-${Date.now()}`),
], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
const stderr = [], commandReplies = new Map();
child.stderr.on('data', (chunk) => stderr.push(chunk.toString()));
const lines = readline.createInterface({ input: child.stdout });
let browser;
const command = (operation) => new Promise((resolve, reject) => {
    const command_id = crypto.randomUUID();
    const timer = setTimeout(() => reject(new Error(`Fixture command timed out: ${operation}`)), 8000);
    commandReplies.set(command_id, () => { clearTimeout(timer); resolve(); });
    child.stdin.write(JSON.stringify({ command_id, operation }) + '\n');
});

async function bounds(page) {
    const result = await page.evaluate(() => {
        const main = document.querySelector('.page-scroll').getBoundingClientRect();
        const header = document.querySelector('.topbar').getBoundingClientRect();
        const footer = document.querySelector('.workspace-footer').getBoundingClientRect();
        const clipped = [...document.querySelectorAll('button:not(:disabled),.market-permission')].filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height && element.scrollWidth > element.clientWidth + 1;
        }).map((element) => element.textContent.trim());
        return { width: innerWidth, height: innerHeight, documentWidth: document.documentElement.scrollWidth,
            mainTop: main.top, mainBottom: main.bottom, headerTop: header.top,
            headerBottom: header.bottom, footerTop: footer.top, footerBottom: footer.bottom, clipped };
    });
    assert.equal(result.width, result.documentWidth);
    assert.equal(result.headerTop, 0);
    assert.equal(result.mainTop, result.headerBottom);
    assert.equal(result.mainBottom, result.footerTop);
    assert.equal(result.footerBottom, result.height);
    assert.deepEqual(result.clipped, []);
}

try {
    const ready = await Promise.race([
        new Promise((resolve, reject) => {
            lines.on('line', (line) => {
                try {
                    const value = JSON.parse(line);
                    if (value.ui) resolve(value);
                    if (commandReplies.has(value.command_id)) {
                        commandReplies.get(value.command_id)();
                        commandReplies.delete(value.command_id);
                    }
                } catch { /* Ignore non-ready fixture diagnostics. */ }
            });
            child.on('exit', (code) => reject(new Error(`Fixture exited ${code}: ${stderr.join('')}`)));
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('Fixture startup timed out')), 12000)),
    ]);
    browser = await playwright.chromium.launch({
        headless: true,
        ...(process.env.NYXION_TEST_BROWSER ? { executablePath: process.env.NYXION_TEST_BROWSER } : {}),
    });
    const origin = `http://127.0.0.1:${ready.ui[1]}`;
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 }, colorScheme: 'light' });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [], mutations = [], external = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        const url = new URL(request.url());
        if (url.origin !== origin) external.push(url.origin);
        if (request.method() !== 'GET') mutations.push({
            method: request.method(), path: url.pathname, body: request.postData(),
        });
    });
    const management = (route) => page.evaluate(async (route) => {
        const response = await fetch(route, {
            headers: { 'X-Heredity-Token': localStorage.getItem('heredity_token') },
        });
        return response.json();
    }, route);
    await page.goto(origin + '/market');
    await page.getByLabel('访问密码').fill('market-browser-fixture-2026');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('heading', { name: '模块商场', exact: true }).waitFor();
    await page.locator('[data-market-plugin-id="qa.market-active"]').waitFor();
    await page.getByText('不完整目录示例', { exact: true }).waitFor();
    await page.getByText('无法安装', { exact: true }).waitFor();
    const card = (id) => page.locator(`[data-market-plugin-id="${id}"]`);
    assert.equal(await card('qa.market-banned').getByRole('button').count(), 0);
    assert.equal(await card('qa.market-future').getByRole('button', { name: '安装', exact: true }).isDisabled(), true);
    assert.equal((await page.locator('.page-content').innerText()).includes('fixture-private-query'), false);
    const support = (await management('/api/heredity/market')).sandbox;
    if ((support.available ?? support.supported) === true) {
        await page.getByText('商场 WASM 模块隔离可用', { exact: true }).waitFor();
    } else await page.getByText('商场 WASM 模块隔离未就绪', { exact: true }).waitFor();

    for (const scheme of ['light', 'dark']) {
        await page.emulateMedia({ colorScheme: scheme });
        await bounds(page);
        await page.screenshot({ path: path.join(output, `desktop-${scheme}.png`), animations: 'disabled' });
    }
    await card('qa.market-active').getByRole('button', { name: '安装', exact: true }).click();
    let dialog = page.getByRole('dialog', { name: '安装 离线事件模块', exact: true });
    await dialog.waitFor();
    assert.equal(await dialog.getByRole('button', { name: '确认安装', exact: true }).isDisabled(), true);
    await dialog.getByRole('checkbox', { name: '允许 网络连接 (net.connect)', exact: true }).check();
    assert.equal(await dialog.getByRole('button', { name: '确认安装', exact: true }).isDisabled(), true);
    await dialog.getByRole('checkbox', { name: '允许 读取文件 (fs.read)', exact: true }).check();
    await page.screenshot({ path: path.join(output, 'permissions-desktop-dark.png'), animations: 'disabled' });
    await dialog.getByRole('button', { name: '确认安装', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    let module = page.locator('[data-market-module-id="qa.market-active"]');
    await module.waitFor();
    if ((support.available ?? support.supported) === true) {
        await module.getByText('运行中', { exact: true }).waitFor();
        await module.getByText('独立进程与 WASM 隔离运行中', { exact: true }).waitFor();
        await module.getByRole('button', { name: '重启模块 离线事件模块', exact: true }).click();
        await module.getByText('运行中', { exact: true }).waitFor();
        await module.getByRole('button', { name: '停止模块 离线事件模块', exact: true }).click();
        await module.getByText('已停止', { exact: true }).waitFor();
        await module.getByRole('button', { name: '启动模块 离线事件模块', exact: true }).click();
        await module.getByText('运行中', { exact: true }).waitFor();
    } else await module.getByText('异常', { exact: true }).waitFor();
    const confirmation = mutations.filter((item) => item.path === '/api/heredity/market/install')
        .map((item) => JSON.parse(item.body));
    assert.equal(confirmation.length, 1);
    assert.deepEqual(confirmation[0].confirmed_permissions, ['net.connect', 'fs.read']);
    assert.equal(confirmation[0].expected_version, '1.0.0');
    assert.match(confirmation[0].expected_sha256, /^[0-9a-f]{64}$/);
    assert.equal(confirmation[0].force_warning, false);
    assert.equal(Object.hasOwn(confirmation[0], 'download_url'), false);
    const catalog = await management('/api/heredity/market');
    assert.ok(catalog.plugins.find((item) => item.id === 'qa.market-active').installed);

    await card('qa.market-warning').getByRole('button', { name: '强制安装', exact: true }).click();
    dialog = page.getByRole('dialog', { name: '安装 兼容性警告模块', exact: true });
    await dialog.getByText('测试安全提示：该版本存在兼容性限制。', { exact: true }).waitFor();
    await dialog.getByRole('checkbox', { name: '允许 订阅宿主消息 (bus.subscribe)', exact: true }).check();
    assert.equal(await dialog.getByRole('button', { name: '强制安装', exact: true }).isDisabled(), true);
    await dialog.getByLabel('我已阅读安全提示，仍要强制安装', { exact: true }).check();
    await page.screenshot({ path: path.join(output, 'warning-desktop-dark.png'), animations: 'disabled' });
    await dialog.getByRole('button', { name: '强制安装', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    await page.locator('[data-market-module-id="qa.market-warning"]').waitFor();
    assert.equal(mutations.filter((item) => item.path === '/api/heredity/market/install')
        .map((item) => JSON.parse(item.body)).at(-1).force_warning, true);
    await card('qa.market-corrupt').getByRole('button', { name: '安装', exact: true }).click();
    dialog = page.getByRole('dialog', { name: '安装 校验失败测试模块', exact: true });
    await dialog.getByRole('button', { name: '确认安装', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    await page.locator('.market-job .notice').filter({
        hasText: '安全警告：插件文件哈希校验失败，可能被篡改！',
    }).waitFor();
    assert.equal(await page.locator('[data-market-module-id="qa.market-corrupt"]').count(), 0);

    const phoneContext = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
    const phone = await phoneContext.newPage();
    phone.setDefaultTimeout(10000);
    phone.on('pageerror', (error) => errors.push(error.message));
    await phone.goto(origin + '/market');
    await phone.getByLabel('访问密码').fill('market-browser-fixture-2026');
    await phone.getByRole('button', { name: '登录', exact: true }).click();
    await phone.locator('[data-market-plugin-id="qa.market-active"]').waitFor();
    for (const scheme of ['light', 'dark']) {
        await phone.emulateMedia({ colorScheme: scheme });
        await bounds(phone);
        await phone.screenshot({ path: path.join(output, `mobile-${scheme}.png`), animations: 'disabled' });
    }
    await phone.locator('[data-market-plugin-id="qa.market-corrupt"]')
        .getByRole('button', { name: '安装', exact: true }).click();
    const phoneDialog = phone.getByRole('dialog', { name: '安装 校验失败测试模块', exact: true });
    await phoneDialog.waitFor();
    await bounds(phone);
    await phone.screenshot({ path: path.join(output, 'permissions-mobile-dark.png'), animations: 'disabled' });
    await phoneDialog.getByRole('button', { name: '取消', exact: true }).click();
    await command('fail-refresh');
    await page.getByRole('button', { name: '刷新目录', exact: true }).click();
    await page.locator('.notice').filter({
        hasText: '插件目录刷新失败，保留上次缓存；请稍后手动刷新。',
    }).first().waitFor();
    await page.getByText('缓存目录', { exact: true }).waitFor();
    assert.ok(await card('qa.market-active').count());
    const jobs = (await management('/api/heredity/market')).jobs;
    assert.ok(jobs.some((job) => job.state === 'failed' && job.error?.code === 'PLUGIN_HASH_MISMATCH'));
    assert.ok(!mutations.some((item) => item.path.includes('/plugins/')
        || item.path === '/api/debug/invoke'));
    assert.deepEqual(external, []);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({
        scope: 'real host market jobs, offline package bytes, real WASM runtime',
        package_statuses_verified: ['active', 'warning', 'banned'],
        minimum_version_blocked: true, exact_permissions_confirmed: true,
        actual_hash_failure_visible: true, module_controls_verified: (support.available ?? support.supported) === true,
        invalid_entries_visible: true, failed_refresh_preserved_cache: true,
        browser_external_downloads: 0, page_errors: errors,
        message_send_requests: 0, output,
    }, null, 2));
} finally {
    if (browser) await browser.close();
    if (child.exitCode === null) {
        child.stdin.end('\n');
        await Promise.race([once(child, 'exit'), new Promise((resolve) => setTimeout(resolve, 10000))]);
        if (child.exitCode === null) { child.kill(); await once(child, 'exit'); }
    }
}
