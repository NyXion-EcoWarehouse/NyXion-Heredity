import assert from 'node:assert/strict';
import test from 'node:test';
import { bytes, duration, hostRelease, localOrigin, parseAccounts, parseConnection, parseParams,
    parseProbe, READ_ACTIONS } from '../src/models.ts';

test('account lists preserve platform identities and opaque account IDs', () => {
    const list = [
        { account: 'example:12345678', platform: 'example', self_id: '12345678', nickname: '测试账号' },
        { account: 'im:user-a:device-1', platform: 'im', self_id: 'user-a:device-1', nickname: '' },
    ];
    assert.deepEqual(parseAccounts({ list }), list);
    assert.deepEqual(parseAccounts({ list: [] }), []);
});

test('account lists reject inconsistent identities, duplicates and malformed public values', () => {
    const item = { account: 'example:12345678', platform: 'example', self_id: '12345678', nickname: '测试账号' };
    for (const change of [
        { account: 'im:12345678' }, { platform: 'bad/platform' }, { self_id: '' },
        { nickname: 'line\nbreak' }, { nickname: null }, { account: 'x'.repeat(1025) },
    ]) assert.throws(() => parseAccounts({ list: [{ ...item, ...change }] }));
    assert.throws(() => parseAccounts({ list: [item, item] }));
    assert.throws(() => parseAccounts({ list: null }));
});

test('probe accepts fractional Unix timestamps without inventing identity', () => {
    const value = parseProbe({ probe_id: 'probe', origin: 'http://127.0.0.1:3001',
        self_id: 123456, nickname: 'fixture', online: true,
        execution_level: null, expires_at: 1789000000.125 });
    assert.equal(value.expires_at, 1789000000.125);
    assert.equal(value.self_id, 123456);
    assert.throws(() => parseProbe({ ...value, self_id: 'fake-account' }));
});

test('active event listener is not treated as healthy before an event arrives', () => {
    const value = parseConnection({ configured: true, active: {
        kind: 'external', origin: 'http://127.0.0.1:3001', self_id: 123456,
        nickname: 'fixture', online: true, verified: true, execution_level: 'L1',
        provider_id: 'external', gateway_origin: 'http://127.0.0.1:5701',
        event_ingress: { enabled: true, healthy: null, last_event_at: null,
            accepted: 0, duplicates: 0, reason: 'waiting' },
    } });
    assert.equal(value.active.gateway_origin, 'http://127.0.0.1:5701');
    assert.equal(value.event_ingress.healthy, null);
    assert.equal(value.event_ingress.accepted, 0);
});

test('a local plugin channel remains valid without a listening network gateway', () => {
    const active = {
        kind: 'gateway', origin: null, gateway_origin: null, self_id: 12345678,
        nickname: 'Offline fixture', online: true, verified: true,
        execution_level: null, provider_id: null,
    };
    assert.deepEqual(parseConnection({ configured: false, active }).active, active);
    const network = { ...active, origin: 'http://127.0.0.1:5701',
        gateway_origin: 'http://127.0.0.1:5701' };
    assert.deepEqual(parseConnection({ configured: false, active: network }).active, network);
});

test('external connections require concrete HTTP origins and reject null gateway metadata', () => {
    const active = {
        kind: 'external', origin: 'http://127.0.0.1:3001', gateway_origin: 'http://127.0.0.1:5701',
        self_id: 87654321, nickname: 'Offline external fixture', online: true, verified: true,
        execution_level: 'L1', provider_id: 'external',
    };
    assert.deepEqual(parseConnection({ configured: true, active }).active, active);
    for (const change of [
        { origin: null }, { gateway_origin: null }, { origin: '' }, { origin: 'not-a-url' },
        { origin: 'javascript:alert(1)' }, { origin: 'http://user:secret@127.0.0.1:3001' },
        { gateway_origin: 'http://127.0.0.1:5701/path' }, { gateway_origin: 5701 },
    ]) assert.throws(() => parseConnection({ configured: true, active: { ...active, ...change } }));
});

test('explicit origins allow only local HTTP(S) origins and no tokens in URLs', () => {
    assert.equal(localOrigin(' http://127.0.0.1:3001/ '), 'http://127.0.0.1:3001');
    assert.equal(localOrigin('http://localhost:5700'), 'http://localhost:5700');
    for (const value of ['https://example.com:3001', 'http://user:secret@127.0.0.1:3001',
        'http://127.0.0.1:3001/action', 'http://127.0.0.1:3001?token=secret', 'file:///application']) {
        assert.throws(() => localOrigin(value));
    }
});

test('writes require distinct confirmation and JSON params cannot be arrays', () => {
    assert.ok(READ_ACTIONS.has('get_status'));
    for (const action of ['send_private_msg', 'send_group_msg', 'delete_msg', 'unknown']) {
        assert.ok(!READ_ACTIONS.has(action));
    }
    assert.deepEqual(parseParams('{"message_id":-123}'), { message_id: -123 });
    assert.throws(() => parseParams('[]'));
    assert.throws(() => parseParams('{broken'));
});

test('unknown metrics stay unknown while measured zero stays zero', () => {
    assert.equal(bytes(undefined), '—');
    assert.equal(bytes(null), '—');
    assert.equal(bytes(0), '0 B');
    assert.equal(bytes(1024), '1.0 KB');
    assert.equal(duration(undefined), '—');
    assert.equal(duration(3665), '1 小时 1 分');
});

test('release labels remain separate from machine versions and support older hosts', () => {
    assert.deepEqual(hostRelease({ version: '26.11.13', version_label: 'Alpha Build 26.11.13' }), {
        version: '26.11.13', label: 'Alpha Build 26.11.13',
    });
    assert.deepEqual(hostRelease({ version: '0.3.0' }), { version: '0.3.0', label: 'v0.3.0' });
    assert.deepEqual(hostRelease({ version: 'development' }), {
        version: 'development', label: 'development',
    });
    const fallback = hostRelease({});
    assert.deepEqual(hostRelease({ version: null, version_label: '\n' }, fallback), fallback);
    assert.equal(fallback.version, '26.11.13.post1');
    assert.equal(fallback.label, 'Alpha Build 26.11.13 (Core Revision 1)');
    assert.equal(fallback.label.startsWith('vAlpha'), false);
});
