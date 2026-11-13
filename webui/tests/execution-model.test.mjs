import assert from 'node:assert/strict';
import test from 'node:test';
import {
    availabilityLabel, canSelectProvider, parseExecutionPayload, parseSwitchResult,
    provenanceLabel, shortEpoch, sourceLabel,
} from '../execution-model.ts';

const provider = (changes = {}) => ({
    provider_id: 'demo', label: 'Demo provider', execution_level: null, source_kind: 'demo',
    host_kind: 'fixture', session_owner: 'fixture', target_memory_access: 'none',
    in_process_code: false, business_request_builder: 'demo',
    primary_transport_owner: 'fixture', auxiliary_transports: [], event_sources: [],
    supported_actions: ['get_status'], receipt_evidence: [], availability: 'ready', reason: '',
    level_provenance: 'none', selectable: true, ...changes,
});
const state = (changes = {}) => ({
    state: 'ready', active_provider_id: 'demo', session_epoch: 'fixture-session-epoch',
    descriptor: provider(), providers: [provider()], in_flight: 0, ...changes,
});
const payload = (changes = {}) => ({
    levels: ['L0', 'L1', 'L2', 'L3'].map((level) => ({ level, label: level, description: 'Fixture level' })),
    state: state(), canSwitch: true, ...changes,
});

test('provider catalog keeps a demo outside execution levels and preserves supported actions', () => {
    const result = parseExecutionPayload(payload());
    assert.equal(result.state.descriptor.execution_level, null);
    assert.equal(result.state.descriptor.source_kind, 'demo');
    assert.deepEqual(result.state.descriptor.supported_actions, ['get_status']);
    assert.deepEqual(result.levels.map((entry) => entry.level), ['L0', 'L1', 'L2', 'L3']);
    assert.equal(result.canSwitch, true);
});

test('an external declaration remains declared rather than implementation evidence', () => {
    const external = provider({ provider_id: 'external', source_kind: 'profile',
        execution_level: 'L1', level_provenance: 'declared' });
    const result = parseExecutionPayload(payload({
        state: state({ providers: [provider(), external] }),
    }));
    assert.equal(result.state.providers[1].execution_level, 'L1');
    assert.equal(result.state.providers[1].level_provenance, 'declared');
    assert.equal(provenanceLabel('declared'), '配置声明 · 未验证');
    assert.equal(sourceLabel('profile'), '配置接口');
});

test('provider selection requires a ready alternate and an idle management channel', () => {
    const alternate = provider({ provider_id: 'alternate' });
    const runtime = parseExecutionPayload(payload()).state;
    assert.equal(canSelectProvider(alternate, runtime, true, false), true);
    assert.equal(canSelectProvider(provider(), runtime, true, false), false);
    assert.equal(canSelectProvider(alternate, runtime, false, false), false);
    assert.equal(canSelectProvider(alternate, runtime, true, true), false);
    assert.equal(canSelectProvider({ ...alternate, selectable: false }, runtime, true, false), false);
    assert.equal(canSelectProvider({ ...alternate, availability: 'unimplemented' }, runtime, true, false), false);
    for (const phase of ['initializing', 'draining', 'switching', 'closing', 'closed']) {
        assert.equal(canSelectProvider(alternate, { ...runtime, state: phase }, true, false), false);
    }
});

test('switch results preserve the host state and explicit changed flag', () => {
    const result = parseSwitchResult({ state: state(), changed: false });
    assert.equal(result.changed, false);
    assert.equal(result.state.session_epoch, 'fixture-session-epoch');
    assert.throws(() => parseSwitchResult({ state: state(), changed: 'yes' }));
    assert.equal(shortEpoch('short'), 'short');
    assert.equal(shortEpoch('1234567890abcdefghijkl'), '12345678…ijkl');
    assert.equal(availabilityLabel('configuration_required'), '待配置');
});

test('invalid provider metadata and incomplete level catalogs fail explicitly', () => {
    for (const invalid of [
        payload({ levels: [] }),
        payload({ levels: Array(4).fill({ level: 'L0', label: 'L0', description: '' }) }),
        payload({ canSwitch: 'true' }),
        payload({ state: state({ in_flight: -1 }) }),
        payload({ state: state({ providers: [provider(), provider()] }) }),
        payload({ state: state({ active_provider_id: 'missing' }) }),
        payload({ state: state({ descriptor: null }) }),
        payload({ state: state({ descriptor: provider({ in_process_code: 'false' }) }) }),
        payload({ state: state({ descriptor: provider({ source_kind: 'untrusted' }) }) }),
        payload({ state: state({ descriptor: provider({ execution_level: 'L4' }) }) }),
    ]) assert.throws(() => parseExecutionPayload(invalid));
});
