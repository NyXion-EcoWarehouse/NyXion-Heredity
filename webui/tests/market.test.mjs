import assert from 'node:assert/strict';
import test from 'node:test';
import {
    compatibleVersion, isMarketJobActive, marketInstallPayload, marketJobPath,
    marketModulePath, packageCompatible, packageSourceLabel, parseMarketCatalog, parseMarketJob,
    parseMarketModules, parseMarketPackage, permissionDescription,
} from '../src/market-model.ts';

const item = {
    id: 'example.echo', name: '事件回声', description: '模块说明', version: '1.2.0',
    author: { name: 'Example', github: 'https://github.com/example' },
    permissions: ['net.connect', 'fs.read', 'bus.subscribe'],
    download_url: 'https://packages.example.test/echo.zip?signature=must-not-display',
    hash_sha256: 'a'.repeat(64), status: 'active', security_advisory: '',
    min_heredity_version: '0.3.0', installed: false,
};
const job = {
    job_id: 'job-0123456789abcdef', kind: 'install', plugin_id: item.id, state: 'queued',
    created_at: 1794480000, updated_at: 1794480000, result: null, error: null,
};
const catalog = {
    index_url: 'https://raw.githubusercontent.com/example/registry/main/plugins.json',
    last_attempt_at: 1794480000, last_success_at: 1794480001, refreshing: false, stale: false,
    error: null, plugins: [item], invalid_entries: [], jobs: [job],
    sandbox: { available: false, runtime: 'wasmtime', isolation: 'process+wasm',
        reason: '运行依赖暂不可用' },
};

test('market metadata preserves exact identity and exposes only actual isolation availability', () => {
    const parsed = parseMarketCatalog(catalog);
    assert.equal(parsed.plugins[0].id, item.id);
    assert.equal(parsed.plugins[0].hash_sha256, item.hash_sha256);
    assert.equal(parsed.sandbox.available, false);
    assert.equal(parsed.sandbox.reason, '运行依赖暂不可用');
    assert.equal(parseMarketCatalog({ ...catalog, sandbox: {} }).sandbox.available, null);
    assert.equal(parseMarketCatalog({ ...catalog, sandbox: { supported: true } }).sandbox.available, true);
    assert.equal(packageSourceLabel(item.download_url), 'packages.example.test/echo.zip');
    assert.equal(JSON.stringify(permissionDescription('fs.read')).includes('读取文件'), true);
});

test('active installation confirms every declared permission, version and package hash', () => {
    const parsed = parseMarketPackage(item);
    const payload = marketInstallPayload(parsed, '0.3.0', [...item.permissions].reverse(), false);
    assert.deepEqual(payload, {
        plugin_id: item.id, expected_version: item.version, expected_sha256: item.hash_sha256,
        confirmed_permissions: item.permissions, force_warning: false,
    });
    assert.notEqual(payload.confirmed_permissions, parsed.permissions);
    assert.ok(!Object.hasOwn(payload, 'download_url'));
    for (const confirmed of [[], ['fs.read'], [...item.permissions, 'fs.write'],
        ['net.connect', 'fs.read', 'fs.read']]) {
        assert.throws(() => marketInstallPayload(parsed, '0.3.0', confirmed, false));
    }
});

test('warning requires explicit force while banned packages can never be installed', () => {
    const warning = parseMarketPackage({ ...item, status: 'warning', security_advisory: '存在已知问题' });
    assert.throws(() => marketInstallPayload(warning, '0.3.0', item.permissions, false));
    assert.equal(marketInstallPayload(warning, '0.3.0', item.permissions, true).force_warning, true);
    const banned = parseMarketPackage({ ...item, status: 'banned' });
    assert.throws(() => marketInstallPayload(banned, '99.0.0', item.permissions, true));
});

test('host versions use numeric semantic version comparison and honor the verified host result', () => {
    for (const [host, min, expected] of [
        ['0.3.0', '0.3.0', true], ['0.3.0', '1.0.0', false], ['2.10.0', '2.9.0', true],
        ['1.0.0-beta', '1.0.0', false], ['1.0.0', '1.0.0-beta', true],
        ['1.0.0-beta.10', '1.0.0-beta.2', true], ['invalid', '0.3.0', false],
    ]) assert.equal(compatibleVersion(host, min), expected);
    const newer = parseMarketPackage({ ...item, min_heredity_version: '1.0.0' });
    assert.equal(packageCompatible(newer, '0.3.0'), false);
    assert.throws(() => marketInstallPayload(newer, '0.3.0', item.permissions, false));
    assert.equal(packageCompatible({ ...newer, compatible: true }, 'unknown'), true);
});

test('host post-release revisions satisfy stable minimums without changing module version rules', () => {
    assert.equal(compatibleVersion('26.11.13.post1', '26.11.13'), true);
    assert.equal(compatibleVersion('26.11.13.post2', '26.11.13'), true);
    assert.equal(compatibleVersion('26.11.13.post1', '26.11.14'), false);
    assert.equal(compatibleVersion('26.11.13.post1', '26.11.13.post1'), false);
    for (const host of ['26.11.13.post', '26.11.13.post-1', '26.11.13.post01',
        '26.11.13.post1.extra', '26.11.13.post1-beta', '26.11.13.post1+local',
        '26.11.13.post9007199254740992', '26.11.13.unknown']) {
        assert.equal(compatibleVersion(host, '26.11.13'), false, host);
    }
});

test('malformed indexes cannot invent installable identities or hide rejected entries', () => {
    for (const patch of [
        { id: '../outside' }, { hash_sha256: 'abc' }, { hash_sha256: 'A'.repeat(64) },
        { status: 'trusted' }, { permissions: ['fs.read', 'fs.read'] },
        { download_url: 'javascript:alert(1)' }, { download_url: 'https://user:pass@example.test/file.zip' },
        { author: null }, { compatible: 'yes' },
    ]) assert.throws(() => parseMarketPackage({ ...item, ...patch }));
    assert.throws(() => parseMarketCatalog({ ...catalog, plugins: [item, item] }));
    const invalid = { position: 1, name: '不完整条目', error: { code: 'INVALID_MARKET_ENTRY', message: '缺少 id' } };
    const result = parseMarketCatalog({ ...catalog, plugins: [], invalid_entries: [invalid] });
    assert.deepEqual(result.invalid_entries, [invalid]);
    assert.deepEqual(result.plugins, []);
});

test('async jobs preserve queued, failures and installed-not-started without claiming execution', () => {
    assert.equal(isMarketJobActive(parseMarketJob(job)), true);
    assert.equal(isMarketJobActive(parseMarketJob({ ...job, state: 'running' })), true);
    const done = parseMarketJob({ ...job, state: 'completed',
        result: { state: 'installed_not_started', plugin_id: item.id, started: false } });
    assert.equal(isMarketJobActive(done), false);
    assert.equal(done.result.started, false);
    const failed = parseMarketJob({ ...job, state: 'failed',
        error: { code: 'PLUGIN_HASH_MISMATCH', message: '哈希校验失败' } });
    assert.equal(failed.error.code, 'PLUGIN_HASH_MISMATCH');
    assert.equal(marketJobPath(job.job_id), '/api/heredity/market/jobs/job-0123456789abcdef');
    assert.throws(() => marketJobPath('../outside'));
    assert.throws(() => parseMarketJob({ ...job, state: 'done' }));
});

test('installed-record metadata remains compatible with boolean catalog snapshots', () => {
    const parsed = parseMarketPackage({ ...item, installed: { version: '1.1.0', sha256: 'b'.repeat(64) } });
    assert.equal(parsed.installed, true);
    assert.equal(parsed.installed_version, '1.1.0');
    assert.equal(parseMarketPackage({ ...item, installed: true }).installed, true);
});

test('installed WASM module controls retain one namespace and report actual process isolation', () => {
    const module = { id: item.id, name: item.name, version: item.version,
        namespace: `/modules/${item.id}`, state: 'running', permissions: item.permissions,
        sandbox: { runtime: 'wasmtime', isolation: 'process+wasm', isolated: true, process_running: true },
        error: null, installed_at: 1794480000 };
    assert.deepEqual(parseMarketModules({ modules: [module], jobs: [] }).modules, [module]);
    assert.equal(marketModulePath(item.id, 'restart'), '/api/heredity/market/modules/example.echo/restart');
    for (const patch of [{ namespace: '/system' }, { state: 'sandboxed' },
        { sandbox: { ...module.sandbox, isolated: 'true' } }]) {
        assert.throws(() => parseMarketModules({ modules: [{ ...module, ...patch }], jobs: [] }));
    }
    const operation = parseMarketJob({ ...job, kind: 'module_restart', state: 'running' });
    assert.equal(operation.kind, 'module_restart');
    assert.equal(isMarketJobActive(operation), true);
});
