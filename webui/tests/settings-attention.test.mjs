import assert from 'node:assert/strict';
import test from 'node:test';
import { attentionForPath, parseHostAttention } from '../src/attention-model.ts';
import { hostSettingsPayload, parseHostSettings } from '../src/settings-model.ts';

const settings = {
    startup: { ui_host: '127.0.0.1', ui_port: 5109, configured_ui_port: 5110,
        env_file: '.env', restart_required: true },
    services: [
        { interface_id: '0000000000000001', name: '事件接口', type: 'http_server', state: 'running',
            port: 2000, actual_port: 2000, endpoint: 'http://127.0.0.1:2000/onebot', error: null },
        { interface_id: '0000000000000002', name: 'WS 接口', type: 'websocket_server', state: 'stopped',
            port: 9999, actual_port: null, endpoint: null, error: null },
    ], ports_policy: 'per-interface',
};

test('settings preserves current and configured management ports plus every independent service', () => {
    assert.deepEqual(parseHostSettings(settings), settings);
    assert.equal(parseHostSettings({ ...settings, services: [] }).services.length, 0);
    assert.deepEqual(hostSettingsPayload('5109'), { ui_port: 5109 });
    assert.deepEqual(hostSettingsPayload('2000'), { ui_port: 2000 });
    assert.deepEqual(hostSettingsPayload('65535'), { ui_port: 65535 });
    assert.ok(!Object.hasOwn(hostSettingsPayload('9999'), 'services'));
    for (const port of ['', '0', '-1', '65536', '1.5', '5e3', 'NaN', ' 5109 ']) {
        assert.throws(() => hostSettingsPayload(port));
    }
});

test('invalid service metadata cannot hide port conflicts or inject endpoint links', () => {
    for (const change of [
        { ports_policy: 'global' }, { services: [settings.services[0], settings.services[0]] },
        { startup: { ...settings.startup, restart_required: 'yes' } },
        { startup: { ...settings.startup, configured_ui_port: 65536 } },
        { services: [{ ...settings.services[0], type: 'other' }] },
        { services: [{ ...settings.services[0], actual_port: -1 }] },
        { services: [{ ...settings.services[0], endpoint: 'javascript:alert(1)' }] },
    ]) assert.throws(() => parseHostSettings({ ...settings, ...change }));
});

test('attention only projects sanitized findings to the declared application routes', () => {
    const items = [
        { path: '/connections', severity: 'error', count: 2, message: '有 2 个接口启动失败。' },
        { path: '/connections', severity: 'warning', count: 1, message: '有待分配接口。' },
        { path: '/market', severity: 'warning', count: 1, message: '目录包含不可用条目。' },
    ];
    assert.deepEqual(parseHostAttention({ items, healthy: false }), { items, healthy: false });
    assert.deepEqual(attentionForPath(items, '/connections'), {
        severity: 'error', count: 3, message: '有 2 个接口启动失败。；有待分配接口。',
    });
    assert.equal(attentionForPath(items, '/settings'), null);
    assert.deepEqual(parseHostAttention({ items: [], healthy: true }), { items: [], healthy: true });
    const withPrivateDetail = { ...items[0], detail: { token: 'must-not-be-projected' } };
    assert.deepEqual(parseHostAttention({ items: [withPrivateDetail], healthy: false }).items, [items[0]]);
    for (const change of [
        { severity: 'critical' }, { count: 0 }, { count: -1 }, { message: 'line\nbreak' },
    ]) assert.throws(() => parseHostAttention({ items: [{ ...items[0], ...change }], healthy: false }));
    assert.equal(parseHostAttention({ items: [{ ...items[0], path: '/plugins/qa.offline/accounts' }],
        healthy: false }).items[0].path, '/plugins/qa.offline/accounts');
    assert.deepEqual(parseHostAttention({ items: [...items,
        { ...items[0], path: 'https://example.test' }, { ...items[0], path: '/plugins/../accounts' }],
    healthy: false }).items, items);
});

test('plugin attention routes use the generic namespace rule for every adapter', () => {
    for (const path of ['/plugins/example.im/accounts', '/plugins/qa.offline/accounts']) {
        const item = { path, severity: 'warning', count: 1, message: '账号需要重新连接。' };
        assert.deepEqual(parseHostAttention({ items: [item], healthy: false }).items, [item]);
    }
});
