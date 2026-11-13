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
    'ui-multiaccount-qa');
const state = path.join(output, `browser-${Date.now()}`);
await fs.mkdir(output, { recursive: true });
const child = spawn(python, [
    path.join(root, 'webui/tests/plugin-accounts-browser.fixture.py'),
    '--state-dir', state, '--seconds', '90',
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
                catch { /* Only the structured ready record matters. */ }
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
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } });
    const page = await context.newPage();
    page.setDefaultTimeout(6000);
    const errors = [];
    const mutations = [];
    const executionReads = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        if (new URL(request.url()).pathname === '/api/heredity/execution') {
            executionReads.push(request.url());
        }
        if (request.method() !== 'GET') mutations.push({
            method: request.method(), path: new URL(request.url()).pathname,
        });
    });
    await page.goto(origin + '/plugins');
    await page.getByLabel('访问密码').fill('offline-browser-fixture-2026');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    const navigation = page.getByRole('navigation', { name: '主导航' });
    await navigation.getByRole('button', { name: '离线多账号', exact: true }).waitFor();
    await navigation.getByRole('button', { name: '离线多账号', exact: true }).click();
    await page.getByRole('heading', { name: '离线多账号', exact: true }).waitFor();
    await page.getByText('账号认证失败 [FIXTURE_AUTH_FAILURE]：离线诊断示例').first().waitFor();
    assert.equal(await page.getByText('插件交互类型无效', { exact: true }).count(), 0);
    assert.equal(await page.locator('.plugin-section').count(), 3);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 1365);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });

    const rows = page.locator('.plugin-section');
    const first = rows.filter({ has: page.getByRole('heading', { name: '测试账号 A', exact: true }) });
    await first.getByRole('button', { name: '设为 OneBot 当前账号', exact: true }).click();
    await first.getByText('OneBot 当前账号', { exact: true }).waitFor();
    await page.getByRole('button', { name: '恢复自动选择', exact: true }).click();
    await page.getByText('有多个在线账号，请为 OneBot 选择当前账号。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '添加账号', exact: true }).click();
    await page.getByLabel('账号名称', { exact: true }).fill('新增测试账号 C');
    await page.getByRole('button', { name: '添加', exact: true }).click();
    const created = rows.filter({ has: page.getByRole('heading', { name: '新增测试账号 C', exact: true }) });
    await created.waitFor();
    assert.equal(mutations.filter((entry) => entry.path.endsWith('/start')).length, 0);
    const accountInput = created.getByLabel('预期测试账号', { exact: false });
    assert.equal(await accountInput.getAttribute('type'), 'text');
    assert.equal(await accountInput.getAttribute('inputmode'), 'numeric');
    assert.equal(await created.getByLabel('自动登录', { exact: false }).isChecked(), false);
    await accountInput.fill('12345681');
    await created.getByRole('button', { name: '启动', exact: true }).click();
    await created.getByText('在线', { exact: true }).waitFor();
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith('/restart') && response.status() === 200),
        created.getByRole('button', { name: '重启新增测试账号 C', exact: true }).click(),
    ]);
    await created.getByText('在线', { exact: true }).waitFor();
    await created.getByRole('button', { name: '停止', exact: true }).click();
    await created.getByRole('button', { name: '启动', exact: true }).waitFor();
    await page.reload();
    await created.waitFor();
    assert.equal(await created.getByLabel('预期测试账号', { exact: false }).inputValue(), '12345681');
    await created.getByRole('button', { name: '重启新增测试账号 C', exact: true }).waitFor();
    await created.getByLabel('预期测试账号', { exact: false }).fill('12345682');
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith('/api/heredity/plugins') && response.status() === 200),
        page.getByRole('button', { name: '刷新', exact: true }).click(),
    ]);
    assert.equal(await created.getByLabel('预期测试账号', { exact: false }).inputValue(), '12345682');
    await created.getByRole('button', { name: '移除新增测试账号 C', exact: true }).click();
    await created.waitFor({ state: 'detached' });
    assert.equal(await first.getByText('在线', { exact: true }).count(), 1);
    assert.equal(await rows.count(), 3);
    const instanceMutations = mutations.filter((entry) => entry.path.endsWith('/start')
        || entry.path.endsWith('/stop') || entry.path.endsWith('/restart') || entry.method === 'DELETE');
    assert.equal(instanceMutations.length, 4);
    assert.ok(instanceMutations.every((entry) =>
        /^\/api\/heredity\/plugins\/qa\.offline\/instances\/account-[a-z0-9]+/.test(entry.path)));
    assert.ok(mutations.filter((entry) => entry.path.startsWith('/api/heredity/plugins/'))
        .every((entry) => entry.path.startsWith('/api/heredity/plugins/qa.offline/')));
    assert.equal(await navigation.getByRole('button', { name: '执行级别', exact: true }).count(), 0);
    await page.goto(origin + '/execution');
    await page.waitForURL(origin + '/plugins');
    await page.getByRole('heading', { name: '插件', exact: true }).waitFor();
    assert.deepEqual(executionReads, []);

    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
    const phone = await mobile.newPage();
    phone.setDefaultTimeout(6000);
    phone.on('pageerror', (error) => errors.push(error.message));
    await phone.goto(origin + '/plugins/qa.offline/accounts');
    await phone.getByLabel('访问密码').fill('offline-browser-fixture-2026');
    await phone.getByRole('button', { name: '登录', exact: true }).click();
    await phone.getByRole('heading', { name: '离线多账号', exact: true }).waitFor();
    await phone.getByText('账号认证失败 [FIXTURE_AUTH_FAILURE]：离线诊断示例').first().waitFor();
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390);
    await phone.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    await phone.getByRole('button', { name: '打开导航', exact: true }).click();
    await phone.getByRole('navigation').getByRole('button', { name: '离线多账号', exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({
        scope: 'actual host, offline SDK fixtures only', desktop: '1365x1000', mobile: '390x844',
        page_errors: errors, account_instances: 3, scoped_mutations: instanceMutations,
        plugin_execution_route: 'redirected without fetching provider controls',
        output,
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
