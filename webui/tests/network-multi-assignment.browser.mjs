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
const output = path.join(path.resolve(process.env.HEREDITY_BROWSER_OUTPUT ?? path.join(root, '.heredity')),
    'ui-network-multi-qa');
const python = process.env.NYXION_TEST_PYTHON
    ?? path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
await fs.mkdir(output, { recursive: true });
const child = spawn(python, [
    path.join(root, 'webui/tests/network-interfaces-browser.fixture.py'),
    '--state-dir', path.join(output, `fixture-${Date.now()}`),
], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
const stderr = [];
child.stderr.on('data', (chunk) => stderr.push(chunk.toString()));
let browser;

async function checkAppearance(page, scheme, label) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForFunction((expected) =>
        getComputedStyle(document.documentElement).colorScheme === expected, scheme);
    const audit = await page.evaluate(() => {
        const style = getComputedStyle(document.documentElement);
        const value = (name) => style.getPropertyValue(`--${name}`).trim();
        const luminance = (hex) => {
            const raw = hex.replace('#', '');
            const expanded = raw.length === 3 ? [...raw].map((part) => part + part).join('') : raw;
            const channels = expanded.match(/../g).map((part) => parseInt(part, 16) / 255)
                .map((part) => part <= .04045 ? part / 12.92 : ((part + .055) / 1.055) ** 2.4);
            return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
        };
        const pairs = [
            ['foreground', 'page'], ['text-secondary', 'surface'], ['muted-foreground', 'surface'],
            ['text-soft', 'sidebar'], ['primary-foreground', 'primary'],
            ['selection-foreground', 'selection'], ['success', 'success-background'],
            ['warning', 'warning-background'], ['destructive', 'danger-background'],
        ];
        const contrasts = pairs.map(([text, background]) => {
            const first = luminance(value(text)), second = luminance(value(background));
            return { text, background, ratio: (Math.max(first, second) + .05) / (Math.min(first, second) + .05) };
        });
        const overflow = [...document.querySelectorAll(
            'button:not(:disabled), .network-assignment-option, .network-request-grant',
        )].filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height && element.scrollWidth > element.clientWidth + 1;
        }).map((element) => element.textContent.trim());
        return { width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
            primary: value('primary'), accent: value('accent'), contrasts, overflow };
    });
    assert.equal(audit.scrollWidth, audit.width, `${label} has horizontal overflow`);
    assert.deepEqual(audit.overflow, [], `${label} has clipped controls`);
    assert.ok(audit.contrasts.every((pair) => pair.ratio >= 4.5), JSON.stringify(audit.contrasts));
    assert.equal(audit.primary, scheme === 'dark' ? '#59caff' : '#00677c');
    return audit;
}

try {
    const lines = readline.createInterface({ input: child.stdout });
    const ready = await Promise.race([
        new Promise((resolve, reject) => {
            lines.on('line', (line) => {
                try { const value = JSON.parse(line); if (value.ui) resolve(value); }
                catch { /* The fixture may write non-ready diagnostics. */ }
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
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 }, colorScheme: 'light' });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [], mutations = [], appearances = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        if (request.method() !== 'GET') mutations.push({
            method: request.method(), path: new URL(request.url()).pathname, body: request.postData(),
        });
    });
    await page.goto(`${origin}/connections`);
    await page.getByLabel('访问密码').waitFor();
    for (const scheme of ['light', 'dark']) {
        appearances.push(await checkAppearance(page, scheme, `desktop login ${scheme}`));
        await page.screenshot({ path: path.join(output, `login-desktop-${scheme}.png`), fullPage: true, animations: 'disabled' });
    }
    await page.emulateMedia({ colorScheme: 'light' });
    await page.getByLabel('访问密码').fill('network-interfaces-fixture-2026');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('heading', { name: '网络接口', exact: true }).waitFor();
    const management = (route, method = 'GET', body) => page.evaluate(async ({ route, method, body }) => {
        const response = await fetch(route, {
            method,
            headers: { 'X-Heredity-Token': localStorage.getItem('heredity_token'),
                ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        const value = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(value));
        return value;
    }, { route, method, body });
    const initial = await management('/api/heredity/network-interfaces');
    const first = initial.interfaces.find((item) => item.name === '离线 SDK 接口');
    const second = await management('/api/heredity/network-interfaces', 'POST', {
        name: '第二个 HTTP 接口', type: 'http_server', account: 'example:12345678', settings: {},
    });
    const third = await management('/api/heredity/network-interfaces', 'POST', {
        name: '备用 WebSocket 接口', type: 'websocket_server', account: 'example:12345678', settings: {},
    });
    const mismatched = [];
    for (const payload of [
        { name: '不匹配的客户端', type: 'http_client', account: 'example:12345678',
            settings: { url: 'http://127.0.0.1:9/' } },
        { name: '其他账号接口', type: 'http_server', account: 'example:87654321', settings: {} },
    ]) mismatched.push(await management('/api/heredity/network-interfaces', 'POST', payload));
    await management(`/api/heredity/network-interfaces/${second.interface_id}/start`, 'POST', {});
    const accountBefore = (await management('/api/heredity/plugins')).sessions
        .find((item) => item.platform === 'example' && item.self_id === '12345678');
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    const request = page.locator('[data-network-request-id]').filter({ hasText: 'qa.interface-request' });
    const released = page.locator('[data-network-request-id]').filter({ hasText: 'qa.released-request' });
    await request.getByRole('checkbox').first().waitFor();
    assert.equal(await request.getByRole('checkbox').count(), 3);
    assert.equal(await request.getByText('不匹配的客户端', { exact: true }).count(), 0);
    assert.equal(await request.getByText('其他账号接口', { exact: true }).count(), 0);
    await released.getByText('此申请已释放。', { exact: true }).waitFor();
    assert.equal(await released.getByRole('checkbox').count(), 0);
    for (const item of mismatched) {
        await management(`/api/heredity/network-interfaces/${item.interface_id}`, 'DELETE');
    }
    const choose = (item) => request.getByRole('checkbox', {
        name: `为 qa.interface-request 分配 ${item.name} (${item.interface_id})`, exact: true,
    });
    await choose(first).check();
    await choose(second).check();
    await Promise.all([
        page.waitForResponse((response) => response.url().endsWith('/api/heredity/network-requests') && response.status() === 200),
        page.getByRole('button', { name: '刷新', exact: true }).click(),
    ]);
    assert.equal(await choose(first).isChecked(), true);
    assert.equal(await choose(second).isChecked(), true);
    for (const scheme of ['light', 'dark']) {
        appearances.push(await checkAppearance(page, scheme, `desktop assignment ${scheme}`));
        await request.screenshot({ path: path.join(output, `assignment-editor-desktop-${scheme}.png`), animations: 'disabled' });
    }
    await request.getByRole('button', { name: '保存分配', exact: true }).click();
    await request.getByText('已分配 2 个接口', { exact: true }).waitFor();
    const requestId = await request.getAttribute('data-network-request-id');
    const snapshot = async () => (await management('/api/heredity/network-requests')).requests
        .find((item) => item.request_id === requestId);
    const assigned = await snapshot();
    assert.deepEqual(assigned.grants.map((grant) => grant.interface_id), [first.interface_id, second.interface_id]);
    const stable = assigned.grants.find((grant) => grant.interface_id === first.interface_id);
    const credentials = await management(`/api/heredity/network-interfaces/${second.interface_id}/credentials`);
    const endpoint = async (id) => (await management(`/api/heredity/network-interfaces/${id}`)).endpoint;
    const action = async (id, actionName) => {
        const token = id === second.interface_id ? credentials.access_token
            : (await management(`/api/heredity/network-interfaces/${id}/credentials`)).access_token;
        return (await fetch(`${(await endpoint(id)).replace(/\/$/, '')}/${actionName}`, {
            headers: { Authorization: `Bearer ${token}` },
        })).json();
    };
    assert.equal((await action(second.interface_id, 'get_login_info')).status, 'ok');
    await request.getByRole('button', {
        name: `撤销 qa.interface-request 的接口 ${second.interface_id}`, exact: true,
    }).click();
    await request.locator(`[data-grant-interface-id="${second.interface_id}"]`).waitFor({ state: 'detached' });
    assert.equal((await management(`/api/heredity/network-interfaces/${second.interface_id}`)).state, 'running');
    assert.equal((await action(second.interface_id, 'get_login_info')).status, 'failed');
    assert.equal((await action(second.interface_id, 'get_status')).data.state, 'network_interface_unassigned');
    assert.deepEqual((await snapshot()).grants[0], stable);
    assert.equal((await action(first.interface_id, 'get_login_info')).status, 'ok');
    await choose(second).check();
    await request.getByRole('button', { name: '保存分配', exact: true }).click();
    await request.getByText('已分配 2 个接口', { exact: true }).waitFor();
    assert.deepEqual((await snapshot()).grants[0], stable);
    assert.equal((await action(second.interface_id, 'get_login_info')).status, 'ok');
    await request.getByRole('button', { name: '撤销全部', exact: true }).click();
    await request.getByText('已分配 0 个接口', { exact: true }).waitFor();
    assert.equal((await snapshot()).state, 'pending');
    assert.equal((await action(first.interface_id, 'get_login_info')).status, 'failed');
    assert.equal((await action(second.interface_id, 'get_login_info')).status, 'failed');
    await choose(third).check();
    await request.getByRole('button', { name: '保存分配', exact: true }).click();
    await request.getByText('已分配 1 个接口', { exact: true }).waitFor();
    assert.deepEqual((await snapshot()).grants.map((grant) => grant.interface_id), [third.interface_id]);
    await choose(third).uncheck();
    await request.getByRole('button', { name: '保存分配', exact: true }).click();
    await request.getByText('已分配 0 个接口', { exact: true }).waitFor();
    assert.equal((await snapshot()).state, 'pending');
    assert.equal(await request.getByRole('button', { name: '拒绝', exact: true }).count(), 0);
    await management(`/api/heredity/network-requests/${requestId}/deny`, 'POST', {});
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await request.getByText('已拒绝', { exact: true }).waitFor();
    await choose(second).check();
    await request.getByRole('button', { name: '保存分配', exact: true }).click();
    await request.getByText('已分配 1 个接口', { exact: true }).waitFor();
    const accountAfter = (await management('/api/heredity/plugins')).sessions
        .find((item) => item.platform === 'example' && item.self_id === '12345678');
    assert.equal(accountAfter.session_epoch, accountBefore.session_epoch);
    assert.equal(accountAfter.state, 'online');
    assert.ok(!mutations.some((entry) => entry.path.includes('/plugins/') || entry.path === '/api/debug/invoke'));
    for (const scheme of ['light', 'dark']) {
        appearances.push(await checkAppearance(page, scheme, `desktop connections ${scheme}`));
        await page.locator('.page-scroll').evaluate((element) => element.scrollTo(0, 0));
        await page.screenshot({ path: path.join(output, `desktop-${scheme}.png`), fullPage: true, animations: 'disabled' });
    }
    const mobileContext = await browser.newContext({
        viewport: { width: 390, height: 844 }, isMobile: true, colorScheme: 'light',
    });
    const phone = await mobileContext.newPage();
    phone.setDefaultTimeout(8000);
    phone.on('pageerror', (error) => errors.push(error.message));
    await phone.goto(`${origin}/connections`);
    await phone.getByLabel('访问密码').waitFor();
    for (const scheme of ['light', 'dark']) {
        appearances.push(await checkAppearance(phone, scheme, `mobile login ${scheme}`));
        await phone.screenshot({ path: path.join(output, `login-mobile-${scheme}.png`), fullPage: true, animations: 'disabled' });
    }
    await phone.getByLabel('访问密码').fill('network-interfaces-fixture-2026');
    await phone.getByRole('button', { name: '登录', exact: true }).click();
    await phone.getByRole('heading', { name: '网络接口', exact: true }).waitFor();
    const phoneRequest = phone.locator(`[data-network-request-id="${requestId}"]`);
    await phoneRequest.getByText('已分配 1 个接口', { exact: true }).waitFor();
    await phoneRequest.getByRole('checkbox', {
        name: `为 qa.interface-request 分配 ${first.name} (${first.interface_id})`, exact: true,
    }).check();
    for (const scheme of ['light', 'dark']) {
        appearances.push(await checkAppearance(phone, scheme, `mobile assignment ${scheme}`));
        await phoneRequest.screenshot({ path: path.join(output, `assignment-editor-mobile-${scheme}.png`), animations: 'disabled' });
        await phone.locator('.page-scroll').evaluate((element) => element.scrollTo(0, 0));
        await phone.screenshot({ path: path.join(output, `mobile-${scheme}.png`), fullPage: true, animations: 'disabled' });
    }
    await phoneRequest.getByRole('button', { name: '重置选择', exact: true }).click();
    await page.getByRole('navigation').getByRole('button', { name: 'OneBot 调试', exact: true }).click();
    await page.getByRole('heading', { name: 'OneBot 调试', exact: true }).waitFor();
    await page.locator('.debug-fields select').nth(1).selectOption('send_group_msg');
    for (const scheme of ['light', 'dark']) {
        appearances.push(await checkAppearance(page, scheme, `desktop debug ${scheme}`));
        await page.screenshot({ path: path.join(output, `debug-desktop-${scheme}.png`), fullPage: true, animations: 'disabled' });
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({
        scope: 'real local offline host, multiple allocations and brand theme',
        allocation_count: 2, revoke_one_verified: true, revoke_all_verified: true,
        empty_selection_verified: true, denied_reassignment_verified: true,
        stable_unchanged_grant_verified: true, online_epoch_unchanged: true,
        page_errors: errors, minimum_text_contrast: Math.min(...appearances.flatMap(
            (appearance) => appearance.contrasts.map((pair) => pair.ratio))),
        message_send_requests: 0, output,
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
