import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import readline from 'node:readline';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(import.meta.url);
const playwright = require(process.env.PLAYWRIGHT_PACKAGE ?? 'playwright');
const python = process.env.NYXION_TEST_PYTHON
    ?? path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const output = path.join(path.resolve(process.env.HEREDITY_BROWSER_OUTPUT
    ?? path.join(root, '.heredity')), 'ui-host-connections-qa');
await fs.mkdir(output, { recursive: true });

const externalIdentity = { user_id: 87654321, nickname: '离线外部账号' };
const externalToken = 'offline-external-fixture-token';
const externalRequests = [];
const external = createServer(async (request, response) => {
    const endpoint = new URL(request.url, 'http://127.0.0.1').pathname;
    externalRequests.push({ method: request.method, path: endpoint });
    response.setHeader('Content-Type', 'application/json');
    if (request.headers.authorization !== `Bearer ${externalToken}`) {
        response.writeHead(401).end(JSON.stringify({ status: 'failed', retcode: 1401, data: null }));
        return;
    }
    let bytes = 0;
    for await (const chunk of request) {
        bytes += chunk.length;
        if (bytes > 4096) { response.writeHead(413).end(); return; }
    }
    if (request.method !== 'POST' || !['/get_login_info', '/get_status'].includes(endpoint)) {
        response.writeHead(404).end(JSON.stringify({ status: 'failed', retcode: 1404, data: null }));
        return;
    }
    const data = endpoint === '/get_login_info' ? externalIdentity
        : { online: true, good: true, self_id: externalIdentity.user_id };
    response.end(JSON.stringify({ status: 'ok', retcode: 0, data }));
});
external.requestTimeout = 3000;
let child, browser, startupTimer;
const stderr = [];
try {
    external.listen(0, '127.0.0.1');
    await once(external, 'listening');
    const externalOrigin = `http://127.0.0.1:${external.address().port}`;
    child = spawn(python, [
        path.join(root, 'webui/tests/host-connections-browser.fixture.py'),
        '--state-dir', path.join(output, `fixture-${Date.now()}`),
    ], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    child.stderr.on('data', (chunk) => stderr.push(chunk.toString()));
    const lines = readline.createInterface({ input: child.stdout });
    const ready = await Promise.race([
        new Promise((resolve, reject) => {
            lines.on('line', (line) => {
                try { const value = JSON.parse(line); if (value.ui) resolve(value); }
                catch { /* Wait for the ready record only. */ }
            });
            child.on('error', reject);
            child.on('exit', (code) => reject(new Error(`Fixture exited ${code}: ${stderr.join('')}`)));
        }),
        new Promise((_, reject) => {
            startupTimer = setTimeout(() => reject(new Error('Fixture startup timed out')), 10000);
        }),
    ]);
    clearTimeout(startupTimer);
    const origin = `http://127.0.0.1:${ready.ui[1]}`;
    browser = await playwright.chromium.launch({
        headless: true,
        ...(process.env.NYXION_TEST_BROWSER ? { executablePath: process.env.NYXION_TEST_BROWSER } : {}),
    });
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } });
    const page = await context.newPage();
    page.setDefaultTimeout(7000);
    const errors = [], requests = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        requests.push({
            method: request.method(), path: new URL(request.url()).pathname, body: request.postData(),
        });
    });
    const management = (path) => page.evaluate(async (endpoint) => {
        const response = await fetch(endpoint, {
            headers: { 'X-Heredity-Token': localStorage.getItem('heredity_token') },
        });
        if (!response.ok) throw new Error(`Management read failed: ${response.status}`);
        return response.json();
    }, path);

    await page.goto(origin + '/external-connections');
    await page.getByLabel('访问密码').fill('host-connections-fixture-2026');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('heading', { name: '外部 OneBot', exact: true }).waitFor();
    await page.getByRole('navigation').getByRole('button', { name: '外部 OneBot', exact: true }).waitFor();
    await page.locator('.connection-identity').getByText('本地插件通道', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '查看网关配置', exact: true }).isDisabled(), true);
    assert.equal(await page.getByRole('button', { name: '事件回调配置', exact: true }).isDisabled(), true);
    assert.equal(await page.getByLabel('OneBot 网关地址', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('网关令牌', { exact: true }).count(), 0);
    assert.ok(!requests.some((request) => request.path.endsWith('/gateway') || request.path.endsWith('/events/config')));
    const baseline = await management('/api/accounts');
    assert.deepEqual(baseline.list.map(({ account, platform, self_id }) => ({ account, platform, self_id })), [
        { account: 'example:12345678', platform: 'example', self_id: '12345678' },
    ]);

    await page.getByLabel('本机接口地址', { exact: true }).fill(externalOrigin);
    await page.getByLabel('接口令牌', { exact: true }).fill(externalToken);
    await page.getByLabel('声明执行级别', { exact: true }).selectOption('L1');
    await page.getByRole('button', { name: '检测接口', exact: true }).click();
    await page.locator('.probe-result').getByText(externalIdentity.nickname, { exact: true }).waitFor();
    assert.equal((await management('/api/heredity/connection')).configured, false);
    assert.equal(requests.filter((request) => request.path.endsWith('/connection/connect')).length, 0);
    const probeRequest = requests.find((request) => request.path.endsWith('/connection/probe'));
    assert.deepEqual(JSON.parse(probeRequest.body), {
        origin: externalOrigin, access_token: externalToken, execution_level: 'L1',
    });

    await page.getByRole('button', { name: '接通此账号', exact: true }).click();
    await page.getByText('已接通检测确认的账号。', { exact: true }).waitFor();
    await page.locator('.connection-identity').getByText(externalIdentity.nickname, { exact: true }).waitFor();
    assert.equal(await page.getByLabel('接口令牌', { exact: true }).inputValue(), '');
    const connected = await management('/api/heredity/connection');
    assert.equal(connected.configured, true);
    assert.equal(connected.active.kind, 'external');
    assert.equal(connected.active.origin, externalOrigin);
    assert.equal(connected.active.self_id, externalIdentity.user_id);
    assert.equal(connected.active.execution_level, 'L1');
    assert.deepEqual((await management('/api/accounts')).list, [{
        account: `onebot:${externalIdentity.user_id}`, platform: 'onebot',
        self_id: String(externalIdentity.user_id), nickname: externalIdentity.nickname,
    }]);

    await page.getByRole('button', { name: '查看网关配置', exact: true }).click();
    const bridge = await page.getByLabel('OneBot 网关地址', { exact: true }).inputValue();
    const bridgeToken = await page.getByLabel('网关令牌', { exact: true }).inputValue();
    assert.equal(await page.getByLabel('网关令牌', { exact: true }).getAttribute('type'), 'password');
    assert.equal(bridge, connected.active.gateway_origin);
    assert.notEqual(bridge, externalOrigin);
    assert.equal(new URL(bridge).hostname, '127.0.0.1');
    assert.ok(Number(new URL(bridge).port) > 0);
    const identityResponse = await fetch(bridge + '/get_login_info', {
        method: 'POST', body: '{}',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${bridgeToken}` },
    });
    assert.equal(identityResponse.status, 200);
    assert.deepEqual((await identityResponse.json()).data, externalIdentity);
    await page.getByRole('button', { name: '事件回调配置', exact: true }).click();
    const eventUrl = await page.getByLabel('事件回调地址', { exact: true }).inputValue();
    assert.equal(new URL(eventUrl).hostname, '127.0.0.1');
    assert.equal(await page.getByLabel('签名密钥', { exact: true }).getAttribute('type'), 'password');
    assert.ok((await page.getByLabel('签名密钥', { exact: true }).inputValue()).length >= 20);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 1365);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });

    await page.getByRole('navigation').getByRole('button', { name: 'OneBot 调试', exact: true }).click();
    await page.getByRole('heading', { name: 'OneBot 调试', exact: true }).waitFor();
    await page.getByRole('combobox', { name: '账号', exact: true })
        .getByRole('option', { name: `${externalIdentity.nickname} · onebot:${externalIdentity.user_id}`, exact: true })
        .waitFor({ state: 'attached' });
    assert.equal(await page.getByRole('combobox', { name: '账号', exact: true }).inputValue(),
        `onebot:${externalIdentity.user_id}`);
    await page.getByRole('navigation').getByRole('button', { name: '外部 OneBot', exact: true }).click();
    await page.getByRole('heading', { name: '外部 OneBot', exact: true }).waitFor();
    await page.locator('.connection-identity').getByText(externalIdentity.nickname, { exact: true }).waitFor();

    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
    const phone = await mobile.newPage();
    phone.setDefaultTimeout(7000);
    phone.on('pageerror', (error) => errors.push(error.message));
    await phone.goto(origin + '/external-connections');
    await phone.getByLabel('访问密码').fill('host-connections-fixture-2026');
    await phone.getByRole('button', { name: '登录', exact: true }).click();
    await phone.getByRole('heading', { name: '外部 OneBot', exact: true }).waitFor();
    await phone.locator('.connection-identity').getByText(externalIdentity.nickname, { exact: true }).waitFor();
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390);
    await phone.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });

    await page.getByRole('button', { name: '恢复默认网关', exact: true }).click();
    await page.getByText('已恢复默认网关。', { exact: true }).waitFor();
    await page.locator('.connection-identity').getByText('本地插件通道', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '查看网关配置', exact: true }).isDisabled(), true);
    assert.equal(await page.getByRole('button', { name: '事件回调配置', exact: true }).isDisabled(), true);
    assert.equal(await page.getByLabel('OneBot 网关地址', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('网关令牌', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('签名密钥', { exact: true }).count(), 0);
    assert.equal((await management('/api/heredity/connection')).configured, false);
    assert.deepEqual((await management('/api/accounts')).list, baseline.list);
    assert.deepEqual(errors, []);
    const mutations = requests.filter((request) => request.method !== 'GET');
    for (const operation of ['probe', 'connect', 'disconnect']) {
        assert.equal(mutations.filter((request) => request.path === `/api/heredity/connection/${operation}`).length, 1);
    }
    assert.ok(!mutations.some((request) => request.path.includes('/plugins/')
        || request.path === '/api/debug/invoke' || request.path === '/api/heredity/host-connections'));
    assert.ok(externalRequests.length >= 4);
    assert.ok(externalRequests.every((request) => request.method === 'POST'
        && ['/get_login_info', '/get_status'].includes(request.path)));
    console.log(JSON.stringify({
        scope: 'bounded host, offline SDK account, local external OneBot fixture, read actions only',
        desktop: '1365x1000', mobile: '390x844', page_errors: errors,
        message_send_requests: 0, probes: 1, connection_updates: 2,
        external_read_requests: externalRequests.length, output,
    }, null, 2));
} finally {
    clearTimeout(startupTimer);
    if (browser) await browser.close();
    if (child?.pid && child.exitCode === null) {
        child.stdin.end('\n');
        await Promise.race([once(child, 'exit'), new Promise((resolve) => setTimeout(resolve, 10000))]);
        if (child.exitCode === null) { child.kill(); await once(child, 'exit'); }
    }
    if (external.listening) await new Promise((resolve) => external.close(resolve));
}
