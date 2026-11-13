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
    'ui-shell-policy-qa');
await fs.mkdir(output, { recursive: true });
const fixtures = [];
let browser;

async function fixture(file, password) {
    const child = spawn(python, [
        path.join(root, 'webui/tests', file), '--state-dir',
        path.join(output, `fixture-${fixtures.length}-${Date.now()}`),
    ], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    const errors = [], pending = new Map();
    child.stderr.on('data', (chunk) => errors.push(chunk.toString()));
    const lines = readline.createInterface({ input: child.stdout });
    const ready = await Promise.race([
        new Promise((resolve, reject) => {
            lines.on('line', (line) => {
                try {
                    const value = JSON.parse(line);
                    if (value.ui) resolve(value);
                    if (value.command_id && pending.has(value.command_id)) {
                        pending.get(value.command_id)(value);
                        pending.delete(value.command_id);
                    }
                } catch { /* Ignore non-structured diagnostics. */ }
            });
            child.on('exit', (code) => reject(new Error(`Fixture exited ${code}: ${errors.join('')}`)));
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('Fixture startup timed out')), 10000)),
    ]);
    const value = {
        child, origin: `http://127.0.0.1:${ready.ui[1]}`, password,
        command: (operation, module_id = 'qa.interface-request') => new Promise((resolve, reject) => {
            const command_id = crypto.randomUUID();
            const timer = setTimeout(() => {
                pending.delete(command_id);
                reject(new Error(`Fixture command timed out: ${operation}`));
            }, 8000);
            pending.set(command_id, (response) => { clearTimeout(timer); resolve(response); });
            child.stdin.write(JSON.stringify({ command_id, operation, module_id }) + '\n');
        }),
        close: async () => {
            if (child.exitCode !== null) return;
            child.stdin.end('\n');
            await Promise.race([once(child, 'exit'), new Promise((resolve) => setTimeout(resolve, 8000))]);
            if (child.exitCode === null) { child.kill(); await once(child, 'exit'); }
        },
    };
    fixtures.push(value);
    return value;
}

async function login(page, host, route) {
    await page.goto(host.origin + route);
    await page.getByLabel('访问密码').fill(host.password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.locator('.page-scroll').waitFor();
}

async function chromeBounds(page) {
    return page.evaluate(() => {
        const box = (selector) => {
            const element = document.querySelector(selector), rect = element.getBoundingClientRect();
            return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
                height: rect.height, clientHeight: element.clientHeight, scrollHeight: element.scrollHeight };
        };
        return { header: box('.topbar'), footer: box('.workspace-footer'),
            main: box('.page-scroll'), sidebar: box('.sidebar'), sidebarFooter: box('.sidebar-footer'),
            nav: box('.sidebar nav'), width: innerWidth, height: innerHeight,
            documentWidth: document.documentElement.scrollWidth,
            documentHeight: document.documentElement.scrollHeight, windowScroll: scrollY };
    });
}

function assertChrome(value) {
    assert.equal(value.header.top, 0);
    assert.equal(value.header.bottom, value.main.top);
    assert.equal(value.footer.bottom, value.height);
    assert.equal(value.footer.top, value.main.bottom);
    assert.equal(value.documentWidth, value.width);
    assert.equal(value.documentHeight, value.height);
    assert.equal(value.windowScroll, 0);
    assert.ok(value.main.height > 100);
    assert.ok(value.nav.bottom <= value.sidebarFooter.top + 1);
    if (value.width > 760) assert.equal(value.main.left, value.sidebar.right);
    else assert.equal(value.main.left, 0);
}

try {
    browser = await playwright.chromium.launch({
        headless: true,
        ...(process.env.NYXION_TEST_BROWSER ? { executablePath: process.env.NYXION_TEST_BROWSER } : {}),
    });
    const errors = [], mutations = [], historyReads = [];
    let policyPhase = false;
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 }, colorScheme: 'light' });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        const url = new URL(request.url());
        if (policyPhase && request.method() !== 'GET') mutations.push({ path: url.pathname, body: request.postData() });
        if (url.searchParams.get('include_history') === 'true') historyReads.push(url.pathname);
    });
    const shell = await fixture('plugin-accounts-browser.fixture.py', 'offline-browser-fixture-2026');
    let shuffle = false;
    await page.route('**/api/heredity/plugins', async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        if (shuffle) body.navigation.reverse();
        await route.fulfill({ response, json: body });
    });
    await login(page, shell, '/plugins/qa.offline/accounts');
    await page.getByRole('heading', { name: '离线多账号', exact: true }).waitFor();
    const nav = page.getByRole('navigation', { name: '主导航' });
    await nav.getByRole('button', { name: '离线备用入口', exact: true }).waitFor();
    const registrationOrder = await nav.locator('button > span:first-of-type').allTextContents();
    assert.deepEqual(registrationOrder.filter((label) =>
        ['离线多账号', '离线备用入口', '插件'].includes(label)), ['离线多账号', '离线备用入口', '插件']);
    const aligned = await nav.locator('button > span:first-of-type').evaluateAll((elements) =>
        elements.map((element) => element.getBoundingClientRect().left));
    assert.ok(aligned.every((left) => left === aligned[0]));
    shuffle = true;
    await page.waitForResponse((response) => response.url().endsWith('/api/heredity/plugins')
        && response.request().method() === 'GET');
    assert.deepEqual(await nav.locator('button > span:first-of-type').allTextContents(), registrationOrder);
    const layouts = [];
    for (const viewport of [
        { width: 1365, height: 1000 }, { width: 1920, height: 1080 },
        { width: 1365, height: 460 }, { width: 390, height: 844 }, { width: 360, height: 640 },
    ]) {
        await page.setViewportSize(viewport);
        await page.locator('.page-scroll').evaluate((element) => { element.scrollTop = 0; });
        const top = await chromeBounds(page);
        assertChrome(top);
        assert.ok(top.main.scrollHeight > top.main.clientHeight);
        await page.locator('.page-scroll').evaluate((element) => { element.scrollTop = element.scrollHeight; });
        const bottom = await chromeBounds(page);
        assertChrome(bottom);
        assert.deepEqual(bottom.header, top.header);
        assert.deepEqual(bottom.footer, top.footer);
        assert.ok(await page.locator('.page-scroll').evaluate((element) => element.scrollTop > 100));
        layouts.push({ ...viewport, fixed: true });
        await page.screenshot({
            path: path.join(output, `fixed-${viewport.width}x${viewport.height}.png`), animations: 'disabled',
        });
        if (viewport.width <= 760) {
            await page.getByRole('button', { name: '打开导航', exact: true }).click();
            assert.equal(await nav.getByRole('button', { name: '离线备用入口', exact: true }).isVisible(), true);
            await nav.getByRole('button', { name: '离线多账号', exact: true }).click();
        }
    }
    await page.setViewportSize({ width: 1365, height: 1000 });
    const restoredPosition = await page.locator('.page-scroll').evaluate((element) => {
        element.scrollTop = 500;
        return element.scrollTop;
    });
    assert.ok(restoredPosition > 100);
    await nav.getByRole('button', { name: '插件', exact: true }).click();
    await page.getByRole('heading', { name: '插件', exact: true }).waitFor();
    assert.equal(await page.locator('.page-scroll').evaluate((element) => element.scrollTop), 0);
    assertChrome(await chromeBounds(page));
    await page.goBack();
    await page.getByRole('heading', { name: '离线多账号', exact: true }).waitFor();
    await page.waitForFunction((target) =>
        Math.abs(document.querySelector('.page-scroll').scrollTop - target) < 1, restoredPosition);
    assertChrome(await chromeBounds(page));
    await page.unroute('**/api/heredity/plugins');
    await shell.close();

    const host = await fixture('network-interfaces-browser.fixture.py', 'network-interfaces-fixture-2026');
    policyPhase = true;
    await page.evaluate(() => localStorage.removeItem('heredity_token'));
    await login(page, host, '/connections');
    await page.getByRole('heading', { name: '网络接口', exact: true }).waitFor();
    const management = (route, method = 'GET', body) => page.evaluate(async ({ route, method, body }) => {
        const response = await fetch(route, {
            method, headers: { 'X-Heredity-Token': localStorage.getItem('heredity_token'),
                ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(result));
        return result;
    }, { route, method, body });
    const getCurrent = async () => (await management('/api/heredity/network-requests')).requests
        .find((item) => item.module_id === 'qa.interface-request');
    const request = () => page.locator('.network-current-requests [data-network-request-id]')
        .filter({ hasText: 'qa.interface-request' });
    const first = (await management('/api/heredity/network-interfaces')).interfaces[0];
    const initial = await getCurrent();
    assert.equal(initial.auto_start, true);
    assert.equal(initial.saved_interface_ids.length, 0);
    await request().getByRole('switch', { name: '自动启动 qa.interface-request 的接口', exact: true }).waitFor();
    const choose = (row, item) => row.getByRole('checkbox', {
        name: `为 qa.interface-request 分配 ${item.name} (${item.interface_id})`, exact: true,
    });
    await choose(request(), first).check();
    assert.equal(await request().getByRole('switch').isDisabled(), true);
    await request().getByRole('button', { name: '保存分配', exact: true }).click();
    await request().getByText('已保存 1 个接口', { exact: true }).waitFor();
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith('/auto-start') && response.status() === 200),
        request().getByRole('switch').uncheck(),
    ]);
    await page.waitForFunction(() => {
        const input = document.querySelector(
            '.network-current-requests [aria-label="自动启动 qa.interface-request 的接口"]');
        return input && !input.checked && !input.disabled;
    });
    await host.command('stop');
    await management(`/api/heredity/network-interfaces/${first.interface_id}/stop`, 'POST', {});
    const disabledRestart = await host.command('start');
    assert.notEqual(disabledRestart.snapshot.request_id, initial.request_id);
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await request().getByText('已分配 0 个接口', { exact: true }).waitFor();
    assert.equal((await management(`/api/heredity/network-interfaces/${first.interface_id}`)).state, 'stopped');
    assert.deepEqual((await getCurrent()).saved_interface_ids, [first.interface_id]);
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith('/auto-start') && response.status() === 200),
        request().getByRole('switch').check(),
    ]);
    await request().getByText('已分配 1 个接口', { exact: true }).waitFor();
    assert.equal((await management(`/api/heredity/network-interfaces/${first.interface_id}`)).state, 'running');
    assert.deepEqual((await getCurrent()).grants.map((item) => item.interface_id), [first.interface_id]);
    await host.command('release');
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await request().getByRole('button', { name: '重新申请', exact: true }).waitFor();
    const releasedId = (await getCurrent()).request_id;
    assert.equal(historyReads.length, 0);
    await request().getByRole('button', { name: '重新申请', exact: true }).click();
    await request().getByText('已分配 1 个接口', { exact: true }).waitFor();
    const reapplied = await getCurrent();
    assert.notEqual(reapplied.request_id, releasedId);
    assert.equal(await request().count(), 1);
    const inactive = page.locator('.network-current-requests [data-network-request-id]')
        .filter({ hasText: 'qa.inactive-request' });
    assert.equal(await inactive.getByRole('button', { name: '重新申请', exact: true }).isDisabled(), true);
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith('/auto-start') && response.status() === 200),
        inactive.getByRole('switch').uncheck(),
    ]);
    const inactivePolicy = (await management('/api/heredity/network-requests')).requests
        .find((item) => item.module_id === 'qa.inactive-request');
    assert.equal(inactivePolicy.auto_start, false);
    assert.equal(inactivePolicy.module_active, false);
    assert.equal(inactivePolicy.state, 'released');
    assert.deepEqual(inactivePolicy.grants, []);
    const history = page.locator('.network-request-history');
    assert.equal(await history.getAttribute('open'), null);
    await history.locator(':scope > summary').click();
    await history.locator('.network-request-history-item').first().waitFor();
    assert.ok(historyReads.length > 0);
    assert.equal(await history.locator('.network-request-history-item[open]').count(), 0);
    const historyRecords = (await management('/api/heredity/network-requests?include_history=true')).requests;
    assert.ok(historyRecords.filter((item) => item.module_id === 'qa.interface-request').length >= 3);
    assert.equal(await request().count(), 1);
    await history.locator(':scope > summary').click();

    await host.command('stop');
    await management(`/api/heredity/network-interfaces/${first.interface_id}/stop`, 'POST', {});
    await management(`/api/heredity/network-interfaces/${first.interface_id}`, 'DELETE');
    await host.command('start');
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await request().getByRole('button', { name: '重试恢复', exact: true }).waitFor();
    const countBeforeRetry = (await management('/api/heredity/network-interfaces')).interfaces.length;
    await request().getByRole('button', { name: '重试恢复', exact: true }).click();
    await request().getByRole('button', { name: '重试恢复', exact: true }).waitFor();
    assert.equal((await getCurrent()).state, 'pending');
    assert.equal((await management('/api/heredity/network-interfaces')).interfaces.length, countBeforeRetry);
    const replacement = await management('/api/heredity/network-interfaces', 'POST', {
        name: '恢复用 HTTP 接口', type: 'http_server', account: 'example:12345678', settings: {},
    });
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await choose(request(), replacement).check();
    await request().getByRole('button', { name: '保存分配', exact: true }).click();
    await request().getByText('已分配 1 个接口', { exact: true }).waitFor();
    await host.command('stop');
    await host.command('start');
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await request().getByText('已分配 1 个接口', { exact: true }).waitFor();
    assert.equal((await management(`/api/heredity/network-interfaces/${replacement.interface_id}`)).state, 'running');
    assert.deepEqual((await getCurrent()).saved_interface_ids, [replacement.interface_id]);
    await page.waitForFunction(async () => {
        const response = await fetch('/api/heredity/network-requests', {
            headers: { 'X-Heredity-Token': localStorage.getItem('heredity_token') },
        });
        const body = await response.json();
        return body.requests.find((item) => item.module_id === 'qa.interface-request')?.restoring === false;
    });
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await request().locator('fieldset').waitFor({ state: 'visible' });
    for (const viewport of [{ width: 1365, height: 1000 }, { width: 390, height: 844 }]) {
        await page.setViewportSize(viewport);
        for (const scheme of ['light', 'dark']) {
            await page.emulateMedia({ colorScheme: scheme });
            await request().scrollIntoViewIfNeeded();
            assertChrome(await chromeBounds(page));
            await page.screenshot({
                path: path.join(output, `policy-${viewport.width}-${scheme}.png`), animations: 'disabled',
            });
        }
    }
    assert.deepEqual(errors, []);
    assert.ok(!mutations.some((item) => item.path.includes('/plugins/')
        || item.path === '/api/debug/invoke'));
    console.log(JSON.stringify({
        scope: 'offline host shell, saved interface policy and module lifecycle',
        layouts, sidebar_order_stable: true, sidebar_left_alignment: true, main_scroll_restoration: true,
        automatic_start_restored_exact_ids: true, auto_start_can_be_disabled: true,
        reapply_created_new_request: true, inactive_module_blocked: true,
        history_lazy_and_collapsed: true, current_request_deduplicated: true,
        failed_restore_retry_no_fallback: true, page_errors: errors,
        message_send_requests: 0, output,
    }, null, 2));
} finally {
    if (browser) await browser.close();
    for (const host of fixtures) await host.close();
}
