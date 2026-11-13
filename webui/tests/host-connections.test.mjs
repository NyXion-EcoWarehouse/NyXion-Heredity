import assert from 'node:assert/strict';
import test from 'node:test';
import {
    hostConnectionDraft, hostConnectionPayload, parseHostConnections,
} from '../src/host-connections-model.ts';

const settings = {
    schema_version: 1,
    http: { enabled: true, origin: 'http://127.0.0.1:5710', host: '127.0.0.1', port: 5710,
        authentication: { configured: true } },
    sse: { enabled: true, path: '/_heredity/events', url: 'http://127.0.0.1:5710/_heredity/events' },
    event_delivery: { urls: ['http://127.0.0.1:8080/events'], heartbeat_seconds: 15,
        signing_configured: true },
    account_selection: {
        account: 'example:12345678', state: 'ready', accounts: [{
            platform: 'example', self_id: '12345678', plugin_id: 'example.im',
            instance_key: 'default', display_name: '测试账号',
        }],
    },
    supported_transports: ['http', 'http_post', 'sse'],
    unsupported_transports: ['websocket', 'reverse_websocket'], restart_required: false,
};

test('host connection metadata preserves real listeners and account routing', () => {
    assert.deepEqual(parseHostConnections(settings), settings);
    const waiting = parseHostConnections({ ...settings,
        account_selection: { account: null, state: 'waiting_for_plugin', accounts: [] } });
    assert.equal(waiting.account_selection.accounts.length, 0);
    assert.equal(waiting.http.enabled, true);
});

test('drafts contain no stored secrets or mutable snapshot aliases', () => {
    const parsed = parseHostConnections(settings);
    const draft = hostConnectionDraft(parsed);
    assert.equal(draft.accessTokenMode, 'keep');
    assert.equal(draft.eventSecretMode, 'keep');
    assert.equal(draft.accessToken, '');
    assert.equal(draft.eventSecret, '');
    draft.eventUrls.push('http://localhost:9999/new');
    assert.equal(parsed.event_delivery.urls.length, 1);
});

test('keep omits secret fields, replacement and clearing are explicit', () => {
    const draft = hostConnectionDraft(parseHostConnections(settings));
    assert.deepEqual(hostConnectionPayload(draft), {
        event_urls: settings.event_delivery.urls, sse_enabled: true,
    });
    assert.deepEqual(hostConnectionPayload({
        ...draft, accessTokenMode: 'replace', accessToken: 'new-local-token',
        eventSecretMode: 'clear',
    }), { event_urls: settings.event_delivery.urls, sse_enabled: true,
        access_token: 'new-local-token', event_secret: '' });
    assert.deepEqual(hostConnectionPayload({
        ...draft, accessTokenMode: 'clear', eventSecretMode: 'replace', eventSecret: 'new-secret',
    }), { event_urls: settings.event_delivery.urls, sse_enabled: true,
        access_token: '', event_secret: 'new-secret' });
});

test('empty secret replacement never silently clears an existing value', () => {
    const draft = hostConnectionDraft(parseHostConnections(settings));
    for (const value of ['', 'embedded space', 'newline\n', '\x00secret', 'x'.repeat(4097)]) {
        assert.throws(() => hostConnectionPayload({
            ...draft, accessTokenMode: 'replace', accessToken: value,
        }));
        assert.throws(() => hostConnectionPayload({
            ...draft, eventSecretMode: 'replace', eventSecret: value,
        }));
    }
    assert.throws(() => hostConnectionPayload({ ...draft, accessTokenMode: 'unexpected' }));
});

test('event receivers are bounded plain HTTP URLs and are not probe commands', () => {
    const draft = hostConnectionDraft(parseHostConnections(settings));
    assert.deepEqual(hostConnectionPayload({
        ...draft, eventUrls: ['  https://example.test/bot?key=local  '],
    }), { event_urls: ['https://example.test/bot?key=local'], sse_enabled: true });
    for (const url of ['', 'not-a-url', 'ws://localhost:8080/events',
        'http://user:password@localhost:8080/', 'http://localhost:8080/#fragment',
        'http://localhost:8080/\n', 'http://localhost:8080/\\escape']) {
        assert.throws(() => hostConnectionPayload({ ...draft, eventUrls: [url] }));
    }
    assert.throws(() => hostConnectionPayload({
        ...draft, eventUrls: Array(101).fill('http://localhost:8080/events'),
    }));
    assert.throws(() => hostConnectionPayload({
        ...draft, eventUrls: ['http://localhost:8080/events', 'http://localhost:8080/events'],
    }));
});

test('malformed snapshots cannot invent supported transport state or endpoint links', () => {
    for (const change of [{ schema_version: 2 },
        { http: { ...settings.http, port: 65536 } },
        { http: { ...settings.http, enabled: 'true' } },
        { http: { ...settings.http, authentication: { configured: 'yes' } } },
        { http: { ...settings.http, origin: 'javascript:alert(1)' } },
        { http: { ...settings.http, origin: 'http://localhost:5710/path' } },
        { sse: { ...settings.sse, url: 'http://another.test/_heredity/events' } },
        { sse: { ...settings.sse, path: '/different' } },
        { event_delivery: { ...settings.event_delivery, heartbeat_seconds: Infinity } },
        { account_selection: { ...settings.account_selection, accounts: {} } },
        { unsupported_transports: 'websocket' }]) {
        assert.throws(() => parseHostConnections({ ...settings, ...change }));
    }
});
