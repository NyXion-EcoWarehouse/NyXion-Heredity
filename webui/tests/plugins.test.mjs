import assert from 'node:assert/strict';
import test from 'node:test';
import {
    configFields, configInputValues, configPayload, parseAccountSelection, parsePluginCatalog, parsePluginChallenge,
    mergePluginNavigation, pluginInstancePath,
} from '../src/plugin-model.ts';

const plugin = {
    id: 'example.im', name: 'Example IM', version: '0.1.0', platform: 'example', execution: 'embedded',
    state: 'running', handshake: { api_major: 1, api_minor: 0, verified: true },
    heartbeat: { sequence: 1, last_seen_at: 1794430000.5, lease_seconds: 30, healthy: true },
    session: { platform: 'example', self_id: '12345678', session_epoch: 'fixture-epoch',
        state: 'authenticating', reason: 'Waiting for scan' },
    actions: ['get_login_info'], capabilities: ['text'], error: null,
    config_schema: { type: 'object', required: ['expected_self_id'], properties: {
        expected_self_id: { type: 'integer', minimum: 1, maximum: 4294967295 },
        profile_version: { type: 'integer', enum: [1, 2], default: 1 },
        timeout: { type: 'number', minimum: 0.1, maximum: 120, default: 12 },
        enabled: { type: 'boolean', default: true },
        token: { type: 'string', writeOnly: true, default: 'must-never-prefill' },
    } },
};
const catalog = { api_version: '1.0', host_running: true, plugins: [plugin], sessions: [plugin.session] };

test('host handshake health does not invent an online platform session', () => {
    const parsed = parsePluginCatalog(catalog);
    assert.equal(parsed.plugins[0].heartbeat.healthy, true);
    assert.equal(parsed.plugins[0].session.state, 'authenticating');
    assert.equal(parsed.plugins[0].session.self_id, '12345678');
    assert.equal(parsed.plugins[0].heartbeat.last_seen_at, 1794430000.5);
});

test('standalone management UI represents an absent host without a fake plugin', () => {
    assert.deepEqual(parsePluginCatalog({ api_version: '1.0', host_running: false,
        plugins: [], sessions: [] }), { api_version: '1.0', host_running: false, plugins: [], sessions: [],
        navigation: [] });
});

test('prelogin plugin state can exist without an authenticated account identity', () => {
    const parsed = parsePluginCatalog({ ...catalog, plugins: [{ ...plugin, session: {
        ...plugin.session, self_id: null, session_epoch: null, state: 'login_required' } }] });
    assert.equal(parsed.plugins[0].session.self_id, null);
    assert.equal(parsed.plugins[0].session.state, 'login_required');
});

test('invalid versions, duplicate plugin IDs and malformed session state fail explicitly', () => {
    assert.throws(() => parsePluginCatalog({ ...catalog, api_version: '2.0' }));
    assert.throws(() => parsePluginCatalog({ ...catalog, plugins: [plugin, plugin] }));
    assert.throws(() => parsePluginCatalog({ ...catalog,
        plugins: [{ ...plugin, session: { ...plugin.session, state: 'maybe' } }] }));
    assert.throws(() => parsePluginCatalog({ ...catalog,
        plugins: [{ ...plugin, heartbeat: { ...plugin.heartbeat, last_seen_at: 'unknown' } }] }));
});

test('configuration controls preserve typed defaults and never prefill secrets', () => {
    const fields = configFields(plugin.config_schema);
    assert.equal(fields.find((field) => field.key === 'token').defaultValue, '');
    const values = configPayload(fields, { expected_self_id: '12345678' });
    assert.deepEqual(values, { expected_self_id: 12345678, profile_version: 1, timeout: 12, enabled: true });
    assert.throws(() => configPayload(fields, {}));
    for (const value of ['0', '-1', '4294967296', '1.5', 'Infinity']) {
        assert.throws(() => configPayload(fields, { expected_self_id: value }));
    }
    assert.throws(() => configPayload(fields, { expected_self_id: '123', profile_version: '99' }));
    assert.throws(() => configFields({ type: 'object', properties: { hidden: { type: 'object' } } }));
});

test('saved non-secret configuration refills each account without overriding local edits', () => {
    const saved = { expected_self_id: 12345678, profile_version: 2, enabled: false,
        token: 'must-never-prefill' };
    const parsed = parsePluginCatalog({ ...catalog, plugins: [{
        ...plugin, config_values: saved, has_saved_config: true,
    }] }).plugins[0];
    assert.deepEqual(parsed.config_values,
        { expected_self_id: 12345678, profile_version: 2, enabled: false });
    assert.equal(parsed.has_saved_config, true);
    const inputs = configInputValues(parsed.config_values);
    assert.deepEqual(inputs, { expected_self_id: '12345678', profile_version: '2', enabled: false });
    assert.equal(configPayload(configFields(plugin.config_schema), {
        ...inputs, expected_self_id: '87654321',
    }).expected_self_id, 87654321);
    const legacy = parsePluginCatalog(catalog).plugins[0];
    assert.deepEqual(legacy.config_values, {});
    assert.equal(legacy.has_saved_config, false);
});

test('challenge parser only permits inline PNG data and keeps absent challenges absent', () => {
    assert.equal(parsePluginChallenge({ challenge: null }), null);
    const encoded = Buffer.from('\x89PNG\r\n\x1a\nfixture', 'latin1').toString('base64');
    const challenge = { challenge_id: 'fixture', kind: 'qr', title: 'Scan', expires_at: 1794430100,
        payload: { mime: 'image/png', image_base64: encoded, message: 'Waiting' } };
    const value = parsePluginChallenge({ challenge });
    assert.equal(value.image, ['data:', 'image/png', ';base64,', encoded].join(''));
    assert.equal(value.message, 'Waiting');
    for (const payload of [{ mime: 'image/svg+xml', image_base64: encoded },
        { mime: 'image/png', image_base64: 'not-an-image' }]) {
        assert.throws(() => parsePluginChallenge({ challenge: { ...challenge, payload } }));
    }
});

test('the SDK notice challenge and starting session are valid public states', () => {
    const result = parsePluginChallenge({ challenge: {
        challenge_id: 'failure', kind: 'notice', title: 'Account login',
        expires_at: null, payload: { status: 'failed',
            message: '账号认证失败 [FIXTURE_AUTH_FAILURE]' },
    } });
    assert.equal(result.kind, 'notice');
    assert.equal(result.image, null);
    assert.match(result.message, /FIXTURE_AUTH_FAILURE/);
    const parsed = parsePluginCatalog({ ...catalog, plugins: [{
        ...plugin, session: { ...plugin.session, state: 'starting', self_id: null, session_epoch: null },
    }] });
    assert.equal(parsed.plugins[0].session.state, 'starting');
    assert.throws(() => parsePluginChallenge({ challenge: {
        challenge_id: 'bad', kind: 'arbitrary-script', title: 'bad', payload: {},
    } }));
});

const page = { id: 'accounts', title: '示例账号', path: '/plugins/example.im/accounts',
    view: 'accounts', icon: 'users' };
const first = { ...plugin, instance_key: 'default', instance_id: 'runtime-default',
    display_name: '默认账号', is_default: true };
const second = { ...plugin, instance_key: 'account-test', instance_id: 'runtime-second',
    display_name: '测试账号 B', is_default: false,
    session: { ...plugin.session, self_id: '87654321', session_epoch: 'different-epoch' } };
const multiCatalog = { ...catalog,
    plugins: [{ ...first, instances: [first, second], ui_pages: [page] }],
    navigation: [{ ...page, plugin_id: 'example.im' }],
    sessions: [first.session, second.session] };

test('registered plugin pages produce a sidebar route and independent account instances', () => {
    const parsed = parsePluginCatalog(multiCatalog);
    assert.equal(parsed.navigation[0].path, '/plugins/example.im/accounts');
    assert.equal(parsed.navigation[0].plugin_id, 'example.im');
    assert.deepEqual(parsed.plugins[0].instances.map((instance) =>
        [instance.instance_key, instance.session.self_id]),
    [['default', '12345678'], ['account-test', '87654321']]);
    const withoutNavigation = { ...multiCatalog };
    delete withoutNavigation.navigation;
    assert.equal(parsePluginCatalog(withoutNavigation).navigation[0].path, page.path);
});

test('unregistered routes, active content, duplicate instances and foreign owners are rejected', () => {
    for (const change of [
        { ui_pages: [{ ...page, path: 'https://outside.invalid' }] },
        { ui_pages: [{ ...page, view: 'javascript' }] },
        { ui_pages: [{ ...page, id: '../accounts', path: undefined }] },
        { ui_pages: [{ ...page, id: 'account_name', path: undefined }] },
        { instances: [first, first] },
        { instances: [{ ...second, id: 'different.plugin' }] },
        { instances: [{ ...second, instance_key: '../other-account' }] },
    ]) {
        assert.throws(() => parsePluginCatalog({
            ...multiCatalog, plugins: [{ ...multiCatalog.plugins[0], ...change }],
        }));
    }
    assert.throws(() => parsePluginCatalog({
        ...multiCatalog, navigation: [{ ...page, plugin_id: 'not.registered' }],
    }));
    assert.throws(() => parsePluginCatalog({
        ...catalog, plugins: [{ ...plugin, id: '../outside', ui_pages: [{ ...page, path: undefined }] }],
    }));
    assert.throws(() => parsePluginCatalog({
        ...multiCatalog, navigation: [{ ...page, id: 'undeclared', plugin_id: 'example.im',
            path: '/plugins/example.im/undeclared' }],
    }));
});

test('legacy catalog still maps only its default instance without invented child navigation', () => {
    const parsed = parsePluginCatalog(catalog);
    assert.equal(parsed.plugins[0].instances.length, 1);
    assert.equal(parsed.plugins[0].instances[0].instance_key, 'default');
    assert.deepEqual(parsed.plugins[0].ui_pages, []);
    assert.deepEqual(parsed.navigation, []);
});

test('instance API paths remain inside one reviewed plugin and logical account', () => {
    assert.equal(pluginInstancePath('example.im', 'account-test'),
        '/api/heredity/plugins/example.im/instances/account-test');
    for (const key of ['../other', 'a/b', 'A account', '', '..']) {
        assert.throws(() => pluginInstancePath('example.im', key));
    }
    assert.throws(() => pluginInstancePath('a/b', 'default'));
});

test('plugin navigation retains registration order across shuffled polls and appends new entries', () => {
    const first = { ...page, plugin_id: 'example.im' };
    const second = { ...page, id: 'second', title: 'Second', path: '/plugins/example.im/second',
        plugin_id: 'example.im' };
    const third = { ...second, id: 'third', title: 'Third', path: '/plugins/example.im/third' };
    assert.deepEqual(mergePluginNavigation([], [first, second]), [first, second]);
    assert.deepEqual(mergePluginNavigation([first, second], [second, first]), [first, second]);
    assert.deepEqual(mergePluginNavigation([first, second], [third, second, { ...first, title: 'Updated' }]),
        [{ ...first, title: 'Updated' }, second, third]);
    assert.deepEqual(mergePluginNavigation([first, second], [second]), [second]);
});

test('OneBot selection preserves an explicit account and represents ambiguous automatic routing', () => {
    const selection = { account: 'example:12345678',
        accounts: [{ platform: 'example', self_id: '12345678' }, { platform: 'example', self_id: '87654321' }],
        state: 'ready' };
    assert.deepEqual(parseAccountSelection(selection), selection);
    assert.equal(parseAccountSelection({ ...selection, account: null,
        state: 'account_selection_required' }).account, null);
    assert.throws(() => parseAccountSelection({ ...selection, account: 'http://outside.invalid' }));
    assert.throws(() => parseAccountSelection({ ...selection, accounts: null }));
});
