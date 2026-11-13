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
    'ui-network-interfaces-qa');
await fs.mkdir(output, { recursive: true });
const child = spawn(python, [
    path.join(root, 'webui/tests/network-interfaces-browser.fixture.py'),
    '--state-dir', path.join(output, `fixture-${Date.now()}`),
], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
const stderr = [];
child.stderr.on('data', (chunk) => stderr.push(chunk.toString()));
let browser;
try {
    const lines = readline.createInterface({ input: child.stdout });
    const ready = await Promise.race([
        new Promise((resolve, reject) => {
            lines.on('line', (line) => {
                try { const value = JSON.parse(line); if (value.ui) resolve(value); }
                catch { /* Ignore non-ready logs. */ }
            });
            child.on('exit', (code) => reject(new Error(`Fixture exited ${code}: ${stderr.join('')}`)));
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('Fixture startup timed out')), 10000)),
    ]);
    const origin = `http://127.0.0.1:${ready.ui[1]}`;
    browser = await playwright.chromium.launch({
        headless: true,
        ...(process.env.NYXION_TEST_BROWSER ? { executablePath: process.env.NYXION_TEST_BROWSER } : {}),
    });
    const context = await browser.newContext({
        viewport: { width: 1365, height: 1000 },
        colorScheme: 'light',
        permissions: ['clipboard-read', 'clipboard-write'],
    });
    const page = await context.newPage();
    page.setDefaultTimeout(7000);
    const errors = [];
    const mutations = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        if (request.method() !== 'GET') mutations.push({
            method: request.method(), path: new URL(request.url()).pathname,
            body: request.postData(),
        });
    });
    const getCatalog = () => page.evaluate(async () => {
        const result = await fetch('/api/heredity/network-interfaces', {
            headers: { 'X-Heredity-Token': localStorage.getItem('heredity_token') },
        });
        return result.json();
    });
    await page.goto(origin + '/connections');
    await page.getByLabel('访问密码').fill('network-interfaces-fixture-2026');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('heading', { name: '网络接口', exact: true }).waitFor();
    await page.getByText('本体接口总线运行中', { exact: true }).waitFor();
    await page.getByText('1 个模块 · 1 个订阅', { exact: true }).waitFor();
    await page.getByRole('button', { name: '新建接口', exact: true }).click();
    let dialog = page.getByRole('dialog', { name: '新建网络接口' });
    await dialog.getByLabel('接口名称', { exact: true }).fill('QA HTTP 接口');
    await dialog.locator('input[type="radio"][value="http_server"]').check();
    await dialog.getByLabel('监听端口', { exact: false }).fill('0');
    await dialog.getByLabel('接口路径', { exact: true }).fill('/onebot');
    await dialog.getByLabel('绑定账号', { exact: false }).fill('example:12345678');
    assert.equal(await dialog.getByLabel('接口令牌操作').count(), 0);
    assert.equal(await dialog.getByLabel('新接口令牌', { exact: true }).count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 1365);
    await page.screenshot({ path: path.join(output, 'desktop-dialog.png'), fullPage: true });
    await dialog.getByRole('button', { name: '创建接口', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    let catalog = await getCatalog();
    let created = catalog.interfaces.find((entry) => entry.name === 'QA HTTP 接口');
    assert.ok(created);
    assert.match(created.interface_id, /^[0-9a-f]{16}$/);
    assert.equal(created.state, 'stopped');
    assert.equal(created.enabled, false);
    assert.equal(created.settings.authentication.configured, true);
    let row = page.locator(`[data-interface-id="${created.interface_id}"]`);
    await row.getByRole('button', { name: '查看 QA HTTP 接口 的接口令牌', exact: true }).click();
    const tokenInput = row.getByLabel('QA HTTP 接口 的接口令牌', { exact: true });
    await tokenInput.waitFor();
    let interfaceToken = await tokenInput.inputValue();
    assert.ok(interfaceToken.length >= 32);
    assert.equal(await tokenInput.getAttribute('type'), 'password');
    assert.equal(JSON.stringify(created).includes(interfaceToken), false);
    await row.getByRole('button', { name: '复制 QA HTTP 接口 的接口令牌', exact: true }).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), interfaceToken);
    await row.getByRole('button', { name: '复制 QA HTTP 接口 的接口 ID', exact: true }).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), created.interface_id);
    await row.getByRole('button', { name: '启动', exact: true }).click();
    await row.getByText('运行中', { exact: true }).waitFor();
    catalog = await getCatalog();
    created = catalog.interfaces.find((entry) => entry.interface_id === created.interface_id);
    assert.equal(created.state, 'running');
    assert.ok(created.actual_port > 0);
    const result = await fetch(`${created.endpoint.replace(/\/$/, '')}/get_login_info`, {
        headers: { Authorization: `Bearer ${interfaceToken}` },
    });
    const identity = await result.json();
    assert.equal(result.status, 200);
    assert.equal(identity.data.user_id, 12345678);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    await row.getByRole('button', { name: '编辑 QA HTTP 接口', exact: true }).click();
    dialog = page.getByRole('dialog', { name: '编辑网络接口' });
    await dialog.getByLabel('接口名称', { exact: true }).fill('QA HTTP 已编辑');
    await dialog.getByLabel('接口路径', { exact: true }).fill('/updated');
    assert.equal(await dialog.getByLabel('新接口令牌', { exact: true }).count(), 0);
    await dialog.getByRole('button', { name: '保存配置', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    await row.getByRole('heading', { name: 'QA HTTP 已编辑', exact: true }).waitFor();
    catalog = await getCatalog();
    created = catalog.interfaces.find((entry) => entry.name === 'QA HTTP 已编辑');
    assert.equal(created.interface_id, row ? await row.getAttribute('data-interface-id') : null);
    assert.equal(created.state, 'running');
    assert.equal(created.settings.path, '/updated');
    const moved = await fetch(`${created.endpoint.replace(/\/$/, '')}/get_login_info`, {
        headers: { Authorization: `Bearer ${interfaceToken}` },
    });
    assert.equal(moved.status, 200);
    const originalId = created.interface_id;
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith(`/${originalId}/retry`) && response.status() === 200),
        row.getByRole('button', { name: '重连 QA HTTP 已编辑', exact: true }).click(),
    ]);
    await row.getByRole('button', { name: '重新生成', exact: true }).click();
    await row.getByRole('button', { name: '确认重新生成', exact: true }).click();
    await row.getByRole('button', { name: '确认重新生成', exact: true }).waitFor({ state: 'detached' });
    const regeneratedToken = await row.getByLabel('QA HTTP 已编辑 的接口令牌', { exact: true }).inputValue();
    assert.notEqual(regeneratedToken, interfaceToken);
    interfaceToken = regeneratedToken;
    catalog = await getCatalog();
    created = catalog.interfaces.find((entry) => entry.interface_id === originalId);
    const rotated = await fetch(`${created.endpoint.replace(/\/$/, '')}/get_login_info`, {
        headers: { Authorization: `Bearer ${interfaceToken}` },
    });
    assert.equal(rotated.status, 200);
    const request = page.locator('[data-network-request-id]').filter({ hasText: 'qa.interface-request' });
    await request.getByRole('checkbox', {
        name: `为 qa.interface-request 分配 QA HTTP 已编辑 (${created.interface_id})`, exact: true,
    }).check();
    await request.getByRole('button', { name: '保存分配', exact: true }).click();
    await request.getByText('已分配', { exact: true }).waitFor();
    await request.locator(`[data-grant-interface-id="${created.interface_id}"]`).waitFor();

    const phoneContext = await browser.newContext({
        viewport: { width: 390, height: 844 }, isMobile: true, colorScheme: 'light',
    });
    const phone = await phoneContext.newPage();
    phone.setDefaultTimeout(7000);
    phone.on('pageerror', (error) => errors.push(error.message));
    await phone.goto(origin + '/connections');
    await phone.getByLabel('访问密码').fill('network-interfaces-fixture-2026');
    await phone.getByRole('button', { name: '登录', exact: true }).click();
    await phone.getByRole('heading', { name: '网络接口', exact: true }).waitFor();
    await phone.locator(`[data-interface-id="${created.interface_id}"]`).waitFor();
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390);
    await phone.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    await phone.getByRole('button', { name: '新建接口', exact: true }).click();
    const phoneDialog = phone.getByRole('dialog', { name: '新建网络接口' });
    await phoneDialog.getByLabel('接口名称', { exact: true }).fill('手机布局预览');
    await phoneDialog.locator('input[type="radio"][value="websocket_client"]').check();
    await phoneDialog.getByLabel('远端地址', { exact: true }).fill('ws://127.0.0.1:48080/onebot');
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390);
    await phone.screenshot({ path: path.join(output, 'mobile-dialog.png'), fullPage: true });
    await phoneDialog.getByRole('button', { name: '取消', exact: true }).click();

    await page.emulateMedia({ colorScheme: 'dark' });
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.button-outline'))
        .backgroundColor === 'rgb(35, 39, 53)');
    await page.locator('.page-scroll').evaluate((element) => element.scrollTo(0, 0));
    const logo = page.locator('.sidebar .brand-symbol');
    assert.equal(await logo.evaluate((image) => image.complete && image.naturalWidth > 0), true);
    await page.screenshot({ path: path.join(output, 'desktop-dark.png'), fullPage: true });
    await phone.emulateMedia({ colorScheme: 'dark' });
    await phone.waitForFunction(() => getComputedStyle(document.querySelector('.button-outline'))
        .backgroundColor === 'rgb(35, 39, 53)');
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390);
    await phone.screenshot({ path: path.join(output, 'mobile-dark.png'), fullPage: true });
    await row.getByRole('button', { name: '停止', exact: true }).click();
    await row.getByText('已停止', { exact: true }).waitFor();
    await row.getByRole('button', { name: '删除 QA HTTP 已编辑', exact: true }).click();
    await row.getByRole('button', { name: '确认删除', exact: true }).click();
    await row.waitFor({ state: 'detached' });
    const finalCatalog = await getCatalog();
    assert.ok(!finalCatalog.interfaces.some((entry) => entry.interface_id === created.interface_id));
    assert.ok(!mutations.some((entry) => entry.path.includes('/plugins/')
        || entry.path === '/api/debug/invoke'));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({
        scope: 'real host network CRUD and HTTP interface, offline SDK account',
        desktop: '1365x1000', mobile: '390x844', page_errors: errors,
        message_send_requests: 0,
        interface_id: created.interface_id, interface_crud_verified: true, output,
    }, null, 2));
} finally {
    if (browser) await browser.close();
    if (child.exitCode === null) {
        child.stdin.end('\n');
        await Promise.race([once(child, 'exit'), new Promise((resolve) => setTimeout(resolve, 10000))]);
        if (child.exitCode === null) {
            child.kill();
            await once(child, 'exit');
        }
    }
}
