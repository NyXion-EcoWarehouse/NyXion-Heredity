import assert from 'node:assert/strict';
import test from 'node:test';
import {
    interfaceDraft, interfacePath, interfacePayload, parseInterfaceCredentials,
    parseNetworkInterface, parseNetworkInterfaces, parseRegeneratedCredentials,
} from '../src/network-interfaces-model.ts';

const item = {
    interface_id: '0123456789abcdef', name: '机器人 HTTP',
    type: 'http_server', enabled: true, state: 'running', error: null,
    account: 'example:12345678',
    settings: {
        host: '127.0.0.1', port: 5700, path: '/', heartbeat_seconds: 15,
        sse_enabled: true, authentication: { configured: true }, signing_configured: false,
    },
    endpoint: 'http://127.0.0.1:5700', actual_port: 5700,
    modules: [{ module_id: 'module.bot', subscriptions: 2 }],
    created_at: 1794470000, updated_at: 1794471000, is_primary: true,
};
const types = [
    { type: 'http_server', label: 'HTTP 服务端', implemented: true, description: '监听本地 HTTP 请求。' },
    { type: 'http_client', label: 'HTTP 客户端', implemented: true, description: 'HTTP 事件与动作。' },
    { type: 'websocket_server', label: 'WebSocket 服务端', implemented: true, description: '监听 WebSocket 连接。' },
    { type: 'websocket_client', label: 'WebSocket 客户端', implemented: true, description: '连接 WebSocket 服务。' },
];
const catalog = {
    schema_version: 1, interfaces: [item], types,
    accounts: [{ platform: 'example', self_id: '12345678', display_name: '测试账号' }],
    module_bus: { available: true, id_format: 'hex16' },
};

test('interface metadata preserves the stable ID and per-interface module routing', () => {
    assert.deepEqual(parseNetworkInterface(item), item);
    assert.deepEqual(parseNetworkInterfaces(catalog), catalog);
    assert.equal(interfacePath(item.interface_id),
        '/api/heredity/network-interfaces/0123456789abcdef');
    assert.deepEqual(parseNetworkInterfaces({ ...catalog, interfaces: [], accounts: [] }).interfaces, []);
    assert.equal(parseNetworkInterface({
        ...item, settings: { ...item.settings, url: '' },
    }).settings.url, '');
});

test('drafts preserve listener settings and do not expose stored credentials', () => {
    const draft = interfaceDraft(parseNetworkInterface(item));
    assert.equal(draft.name, item.name);
    assert.equal(draft.port, '5700');
    assert.equal(draft.account, item.account);
    assert.ok(!Object.hasOwn(draft, 'accessToken'));
    assert.ok(!Object.hasOwn(draft, 'eventSecret'));
    assert.deepEqual(interfacePayload(draft, false), {
        name: item.name, account: item.account,
        settings: { host: '127.0.0.1', port: 5700, path: '/',
            heartbeat_seconds: 15, sse_enabled: true },
    });
});

test('create sends no client-generated ID and never automatically starts the interface', () => {
    const draft = { ...interfaceDraft(), name: '新 HTTP 接口' };
    const payload = interfacePayload(draft, true);
    assert.equal(payload.enabled, false);
    assert.equal(payload.type, 'http_server');
    assert.equal(payload.account, null);
    assert.equal(payload.settings.port, 0);
    assert.ok(!Object.hasOwn(payload, 'interface_id'));
    assert.ok(!Object.hasOwn(payload, 'plugin_id'));
});

test('each listener and outbound transport has a precise configuration shape', () => {
    const draft = { ...interfaceDraft(), name: '接口', heartbeatSeconds: '30' };
    assert.deepEqual(interfacePayload({ ...draft, type: 'websocket_server', path: '/onebot' }, true), {
        name: '接口', type: 'websocket_server', enabled: false, account: null,
        settings: { host: '127.0.0.1', port: 0, path: '/onebot', heartbeat_seconds: 30 },
    });
    assert.deepEqual(interfacePayload({
        ...draft, type: 'http_client', url: 'https://example.test/onebot',
    }, true).settings, { url: 'https://example.test/onebot', heartbeat_seconds: 30 });
    assert.deepEqual(interfacePayload({
        ...draft, type: 'websocket_client', url: 'wss://example.test/onebot',
    }, true).settings, { url: 'wss://example.test/onebot', heartbeat_seconds: 30 });
    assert.throws(() => interfacePayload({ ...draft, type: 'http_client', url: 'ws://localhost/onebot' }, true));
    assert.throws(() => interfacePayload({ ...draft, type: 'websocket_client', url: 'https://localhost/onebot' }, true));
});

test('configuration cannot choose or clear host-generated interface credentials', () => {
    const payload = interfacePayload({ ...interfaceDraft(item),
        accessToken: 'must-not-be-sent', eventSecret: 'must-not-be-sent',
        accessTokenMode: 'replace', eventSecretMode: 'clear' }, false);
    assert.ok(!Object.hasOwn(payload.settings, 'access_token'));
    assert.ok(!Object.hasOwn(payload.settings, 'event_secret'));
});

test('explicit credential reads and rotation responses stay scoped to one interface', () => {
    const credentials = { interface_id: item.interface_id, access_token: 'host-generated-token',
        signing_uses_access_token: true };
    assert.deepEqual(parseInterfaceCredentials(credentials, item.interface_id), credentials);
    assert.deepEqual(parseRegeneratedCredentials({
        interface_id: item.interface_id, access_token: 'host-generated-token', interface: item,
    }, item.interface_id), credentials);
    for (const value of ['', 'with space', 'line\n', 'x'.repeat(4097), '\x00hidden']) {
        assert.throws(() => parseInterfaceCredentials({ ...credentials, access_token: value }, item.interface_id));
    }
    assert.throws(() => parseInterfaceCredentials({ ...credentials, interface_id: '0000000000000000' }, item.interface_id));
    assert.throws(() => parseInterfaceCredentials({ ...credentials, signing_uses_access_token: false }, item.interface_id));
    assert.throws(() => parseRegeneratedCredentials({ ...credentials,
        interface: { ...item, interface_id: '0000000000000000' } }, item.interface_id));
});

test('invalid paths, accounts, ports and control-bearing names fail before a mutation', () => {
    const draft = { ...interfaceDraft(), name: '新接口' };
    for (const change of [
        { name: '' }, { name: 'line\nbreak' }, { name: 'x'.repeat(81) },
        { port: '-1' }, { port: '65536' }, { port: '1.5' }, { port: '' },
        { path: 'onebot' }, { path: '//external.test/' }, { path: '/a/../b' },
        { path: '/route?token=key' }, { path: '/route#part' }, { path: '/new\nline' },
        { host: 'http://localhost' }, { host: 'bad host' },
        { account: 'not-a-platform-account' }, { heartbeatSeconds: '0' },
        { heartbeatSeconds: 'Infinity' }, { heartbeatSeconds: '86401' },
    ]) assert.throws(() => interfacePayload({ ...draft, ...change }, true));
});

test('remote addresses cannot inject scripts, credentials or an incompatible protocol', () => {
    const draft = { ...interfaceDraft(), name: '客户端', type: 'http_client' };
    for (const url of [
        '', 'not-a-url', 'javascript:alert(1)', 'file:///tmp/socket',
        'http://user:pass@localhost:123/', 'http://localhost/route#part',
        'http://localhost/route\n', 'http://localhost/\\route', 'http://localhost/route?token=hidden',
    ]) assert.throws(() => interfacePayload({ ...draft, url }, true));
});

test('metadata IDs, transport state and endpoint links are verified independently', () => {
    for (const id of ['123', '0123456789abcdeg', '0123456789ABCDEf', '../outside', 'x'.repeat(16)]) {
        assert.throws(() => parseNetworkInterface({ ...item, interface_id: id }));
        assert.throws(() => interfacePath(id));
    }
    for (const change of [
        { state: 'connected' }, { type: 'plugin_http' }, { enabled: 'true' },
        { endpoint: 'javascript:alert(1)' }, { actual_port: 65536 },
        { modules: [{ module_id: 'module.a', subscriptions: -1 }] },
        { settings: { ...item.settings, authentication: { configured: 'yes' } } },
    ]) assert.throws(() => parseNetworkInterface({ ...item, ...change }));
    assert.throws(() => parseNetworkInterfaces({ ...catalog, interfaces: [item, item] }));
    assert.throws(() => parseNetworkInterfaces({ ...catalog, module_bus: { available: true, id_format: 'uuid' } }));
    assert.throws(() => parseNetworkInterfaces({ ...catalog, types: [types[0], types[0]] }));
});
