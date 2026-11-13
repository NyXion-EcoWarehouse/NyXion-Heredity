import assert from 'node:assert/strict';
import test from 'node:test';
import {
    assignableInterfaces, assignmentPayload, canAssignRequest, currentNetworkRequests,
    historyNetworkRequests, networkRequestPath, parseNetworkRequests, sameInterfaceSelection,
} from '../src/network-requests-model.ts';

const requirement = {
    module_id: 'example.im', transport_types: ['http_server', 'websocket_server'],
    directions: ['call', 'receive'], account: 'example:12345678', purpose: 'OneBot 接入',
    max_instances: 1, max_interfaces: 16,
};
const request = {
    request_id: 'request-0123456789abcdef', module_id: 'example.im', requirement,
    state: 'pending', grant: null, grants: [], error: null, created_at: 1794471000,
    auto_start: true, saved_interface_ids: [], can_reapply: false, module_active: true,
    superseded_by: null, restoring: false,
};

test('module interface requests keep independent host resource requirements', () => {
    assert.deepEqual(parseNetworkRequests({ requests: [request] }), [request]);
    assert.deepEqual(parseNetworkRequests({ requests: [] }), []);
    assert.equal(networkRequestPath(request.request_id),
        '/api/heredity/network-requests/request-0123456789abcdef');
    const grant = {
        request_id: request.request_id, module_id: request.module_id,
        interface_id: '0123456789abcdef', generation: 1, transport_type: 'http_server',
        account: requirement.account, expires_at: null,
    };
    assert.deepEqual(parseNetworkRequests({ requests: [{ ...request, state: 'granted', grant,
        grants: [grant] }] })[0].grant, grant);
    assert.equal(parseNetworkRequests({ requests: [{ ...request, state: 'expired' }] })[0].state, 'expired');
});

test('assignment only offers a declared transport and an available matching account scope', () => {
    const interfaces = [
        { interface_id: '0000000000000001', type: 'http_server', account: null },
        { interface_id: '0000000000000002', type: 'websocket_server', account: requirement.account },
        { interface_id: '0000000000000003', type: 'http_server', account: 'example:87654321' },
        { interface_id: '0000000000000004', type: 'http_client', account: requirement.account },
    ];
    assert.deepEqual(assignableInterfaces(request, interfaces).map((item) => item.interface_id),
        ['0000000000000001', '0000000000000002']);
    assert.equal(assignableInterfaces({ ...request, requirement: { ...requirement, account: null } }, interfaces).length, 3);
});

test('malformed or cross-module allocations do not become assignable UI state', () => {
    for (const change of [
        { request_id: '../outside' }, { module_id: 'other.module' }, { state: 'enabled' },
        { requirement: { ...requirement, transport_types: ['platform_plugin'] } },
        { requirement: { ...requirement, directions: ['admin'] } },
        { requirement: { ...requirement, account: 'other_account' } },
        { state: 'granted' }, { created_at: -1 },
    ]) assert.throws(() => parseNetworkRequests({ requests: [{ ...request, ...change }] }));
    assert.throws(() => parseNetworkRequests({ requests: [request, request] }));
    assert.throws(() => networkRequestPath('a/b'));
    const grant = {
        request_id: request.request_id, module_id: request.module_id,
        interface_id: '0123456789abcdef', generation: 1, transport_type: 'http_server',
        account: requirement.account, expires_at: null,
    };
    for (const change of [
        { module_id: 'other.module' }, { request_id: 'other-request' },
        { interface_id: 'wrong' }, { generation: 0 }, { transport_type: 'http_client' },
        { account: 'example:87654321' },
    ]) assert.throws(() => parseNetworkRequests({ requests: [{ ...request, state: 'granted',
        grant: { ...grant, ...change }, grants: [{ ...grant, ...change }] }] }));
});

test('multiple live grants preserve IDs while legacy single grants receive compatible defaults', () => {
    const first = {
        request_id: request.request_id, module_id: request.module_id,
        interface_id: '0123456789abcdef', generation: 1, transport_type: 'http_server',
        account: requirement.account, expires_at: null, grant_id: 'first-grant',
    };
    const second = { ...first, interface_id: '0000000000000002',
        transport_type: 'websocket_server', grant_id: 'second-grant' };
    const parsed = parseNetworkRequests({ requests: [{
        ...request, state: 'granted', grant: first, grants: [first, second],
        error: 'One previous interface was revoked.',
    }] })[0];
    assert.deepEqual(parsed.grants, [first, second]);
    assert.equal(parsed.grant.interface_id, first.interface_id);
    assert.equal(parsed.error, 'One previous interface was revoked.');
    const legacy = { ...request, state: 'granted', grant: first };
    delete legacy.grants;
    delete legacy.requirement;
    legacy.requirement = { ...requirement };
    delete legacy.requirement.max_interfaces;
    assert.equal(parseNetworkRequests({ requests: [legacy] })[0].requirement.max_interfaces, 16);
    assert.deepEqual(parseNetworkRequests({ requests: [legacy] })[0].grants, [first]);
    for (const patch of [
        { grants: [first, first] }, { grant: second }, { grants: 'invalid' },
        { requirement: { ...requirement, max_interfaces: 1 } },
        { grants: [first, { ...second, module_id: 'other.module' }] },
    ]) assert.throws(() => parseNetworkRequests({ requests: [{
        ...request, state: 'granted', grant: first, grants: [first, second], ...patch,
    }] }));
});

test('reconciliation is an explicit bounded set including empty unassignment and terminal release', () => {
    const selected = ['0000000000000001', '0000000000000002'];
    assert.deepEqual(assignmentPayload(request, selected), { interface_ids: selected });
    assert.notEqual(assignmentPayload(request, selected).interface_ids, selected);
    assert.deepEqual(assignmentPayload(request, []), { interface_ids: [] });
    assert.equal(sameInterfaceSelection(selected, [...selected].reverse()), true);
    assert.equal(sameInterfaceSelection(selected, [selected[0]]), false);
    for (const state of ['pending', 'granted', 'revoked', 'expired', 'denied']) {
        assert.equal(canAssignRequest({ ...request, state }), true);
        assert.doesNotThrow(() => assignmentPayload({ ...request, state }, selected));
    }
    assert.equal(canAssignRequest({ ...request, state: 'released' }), false);
    assert.throws(() => assignmentPayload({ ...request, state: 'released' }, selected));
    for (const ids of [[selected[0], selected[0]], ['../outside'], Array(17).fill(selected[0])]) {
        assert.throws(() => assignmentPayload(request, ids));
    }
});

test('automatic-start policy is explicit and released module lifecycle cannot be reassigned', () => {
    const parsed = parseNetworkRequests({ requests: [{
        ...request, auto_start: false, saved_interface_ids: ['0000000000000001'],
        module_active: false, can_reapply: false,
    }] })[0];
    assert.equal(parsed.auto_start, false);
    assert.deepEqual(parsed.saved_interface_ids, ['0000000000000001']);
    assert.equal(canAssignRequest(parsed), false);
    assert.throws(() => assignmentPayload(parsed, []));
    for (const change of [
        { auto_start: 'true' }, { module_active: 'yes' }, { restoring: 1 }, { can_reapply: 1 },
        { saved_interface_ids: ['bad'] },
        { saved_interface_ids: ['0000000000000001', '0000000000000001'] },
        { superseded_by: '../outside' },
    ]) assert.throws(() => parseNetworkRequests({ requests: [{ ...request, ...change }] }));
});

test('superseded and released duplicates move into history while independent active requirements remain', () => {
    const previous = { ...request, request_id: 'old-request', state: 'released', created_at: 1 };
    const superseded = { ...previous, request_id: 'superseded-request',
        superseded_by: request.request_id };
    const other = { ...request, request_id: 'different-requirement',
        requirement: { ...requirement, directions: ['publish'] } };
    const all = [previous, superseded, request, other];
    assert.deepEqual(currentNetworkRequests(all).map((entry) => entry.request_id),
        [request.request_id, other.request_id]);
    assert.deepEqual(historyNetworkRequests(all, [request, other]).map((entry) => entry.request_id),
        [previous.request_id, superseded.request_id]);
    assert.deepEqual(currentNetworkRequests([previous]).map((entry) => entry.request_id), [previous.request_id]);
    const later = { ...previous, request_id: 'z-latest' };
    assert.deepEqual(currentNetworkRequests([previous, later]).map((entry) => entry.request_id), [later.request_id]);
});
