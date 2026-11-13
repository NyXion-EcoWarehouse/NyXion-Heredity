import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, AlertCircle, ArrowLeftRight, CheckCircle2, Info, RefreshCw } from 'lucide-react';
import { api } from './src/api';
import { Badge, Button } from './src/ui';
import {
    availabilityLabel, canSelectProvider, ExecutionApiError, managerStateLabel,
    parseExecutionPayload, parseSwitchResult, provenanceLabel, shortEpoch, sourceLabel,
    type ExecutionPayload, type ProviderDescriptor,
} from './execution-model';
import './execution-panel.css';

async function requestJson(path: string, options: RequestInit, signal: AbortSignal) {
    try { return await api(path, { ...options, signal }); }
    catch (failure) {
        if (failure instanceof Error && 'status' in failure && typeof failure.status === 'number') {
            throw new ExecutionApiError(failure.status, failure.message);
        }
        throw failure;
    }
}

function Values({ values, empty = '未提供' }: { values: string[]; empty?: string }) {
    return values.length ? <ul className="heredity-execution-values">
        {values.map((value, index) => <li key={`${index}:${value}`}><code>{value}</code></li>)}
    </ul> : <span className="text-muted-foreground">{empty}</span>;
}

function Mechanism({ provider }: { provider: ProviderDescriptor }) {
    const facts = [
        ['宿主类型', provider.host_kind], ['会话所有者', provider.session_owner],
        ['目标内存访问', provider.target_memory_access], ['进程内代码', provider.in_process_code ? '是' : '否'],
        ['业务请求构造者', provider.business_request_builder], ['主传输所有者', provider.primary_transport_owner],
    ];
    return <div className="heredity-execution-mechanism">
        <dl className="heredity-execution-facts">{facts.map(([name, value]) =>
            <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
        <div className="heredity-execution-capabilities">
            <section><h4>辅助传输</h4><Values values={provider.auxiliary_transports} /></section>
            <section><h4>事件来源</h4><Values values={provider.event_sources} /></section>
            <section><h4>支持动作</h4><Values values={provider.supported_actions} empty="未声明支持动作" /></section>
            <section><h4>回执证据</h4><Values values={provider.receipt_evidence} empty="未声明回执证据" /></section>
        </div>
    </div>;
}

function Provider({ provider, payload, busy, switchingId, onSwitch }: {
    provider: ProviderDescriptor; payload: ExecutionPayload; busy: boolean; switchingId: string | null;
    onSwitch: (provider: ProviderDescriptor) => Promise<void>;
}) {
    const active = provider.provider_id === payload.state.active_provider_id;
    return <div className="heredity-execution-provider">
        <div className="heredity-execution-provider-heading">
            <div className="heredity-execution-provider-name"><strong>{provider.label}</strong>
                <Badge variant="secondary">{availabilityLabel(provider.availability)}</Badge>
                {active && <Badge variant="outline">当前活动</Badge>}</div>
            <Button type="button" variant="outline" size="sm"
                disabled={!canSelectProvider(provider, payload.state, payload.canSwitch, busy)}
                aria-busy={switchingId === provider.provider_id} aria-label={`切换到 ${provider.label}`}
                className="heredity-execution-switch" onClick={() => void onSwitch(provider)}>
                <ArrowLeftRight className="size-3.5" />
                {switchingId === provider.provider_id ? '切换中' : active ? '当前活动' : '切换'}
            </Button>
        </div>
        <p className="heredity-execution-provider-meta"><code>{provider.provider_id}</code>
            <span>{sourceLabel(provider.source_kind)}</span><span>{provenanceLabel(provider.level_provenance)}</span></p>
        {provider.reason && <p className="heredity-execution-reason">{provider.reason}</p>}
        <details className="heredity-execution-provider-details"><summary>执行边界、动作与回执</summary>
            <Mechanism provider={provider} /></details>
    </div>;
}

export function ExecutionPanel() {
    const [payload, setPayload] = useState<ExecutionPayload | null>(null);
    const [loading, setLoading] = useState(true);
    const [confirmed, setConfirmed] = useState(false);
    const [switchingId, setSwitchingId] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const readController = useRef<AbortController | null>(null);
    const switchController = useRef<AbortController | null>(null);
    const refresh = useCallback(async (preserveError = false) => {
        readController.current?.abort();
        const controller = new AbortController();
        readController.current = controller;
        const timeout = setTimeout(() => controller.abort('timeout'), 30000);
        setLoading(true); setConfirmed(false);
        if (!preserveError) setError(null);
        try {
            const value = await requestJson('/api/heredity/execution', {}, controller.signal);
            if (!controller.signal.aborted) { setPayload(parseExecutionPayload(value)); setConfirmed(true); }
        } catch (failure) {
            if (controller.signal.aborted && controller.signal.reason !== 'timeout') return;
            const reason = controller.signal.reason === 'timeout' ? '读取执行状态超时'
                : failure instanceof Error ? failure.message : '读取执行状态失败';
            setError((previous) => preserveError && previous ? `${previous}；刷新状态失败：${reason}` : reason);
        } finally {
            clearTimeout(timeout);
            if (readController.current === controller) setLoading(false);
        }
    }, []);
    useEffect(() => {
        void refresh();
        return () => { readController.current?.abort(); switchController.current?.abort(); };
    }, [refresh]);
    const select = async (provider: ProviderDescriptor) => {
        if (switchController.current || !payload
            || !canSelectProvider(provider, payload.state, payload.canSwitch,
                loading || switchingId !== null || !confirmed)) return;
        const controller = new AbortController();
        switchController.current = controller;
        const timeout = setTimeout(() => controller.abort('timeout'), 30000);
        setSwitchingId(provider.provider_id); setError(null); setNotice(null);
        try {
            const result = parseSwitchResult(await requestJson('/api/heredity/execution/switch', {
                method: 'POST', body: JSON.stringify({
                    provider_id: provider.provider_id, expected_epoch: payload.state.session_epoch,
                }),
            }, controller.signal));
            if (!controller.signal.aborted) {
                setPayload((current) => current && { ...current, state: result.state }); setConfirmed(true);
                setNotice(result.changed ? `已切换到 ${result.state.descriptor?.label ?? provider.label}`
                    : '提供方未发生变更');
            }
        } catch (failure) {
            if (controller.signal.aborted && controller.signal.reason !== 'timeout') return;
            setConfirmed(false);
            setError(controller.signal.reason === 'timeout'
                ? '切换请求超时，结果未确认。请刷新状态后再操作。'
                : `${failure instanceof Error ? failure.message : '切换失败'}。请刷新状态后再操作。`);
            if (failure instanceof ExecutionApiError && failure.status === 409) await refresh(true);
        } finally {
            clearTimeout(timeout);
            if (switchController.current === controller) {
                switchController.current = null; setSwitchingId(null);
            }
        }
    };
    const busy = loading || switchingId !== null;
    const descriptor = payload?.state.descriptor;
    const unassigned = payload?.state.providers.filter((provider) => provider.execution_level === null) ?? [];
    return <div className="heredity-execution">
        <header className="heredity-execution-page-heading">
            <div><h2>执行级别</h2><p>提供方的执行边界、可用能力与会话状态。</p></div>
            <Button type="button" variant="outline" size="sm" disabled={busy}
                onClick={() => { setNotice(null); void refresh(); }}>
                <RefreshCw className={`size-3.5 ${loading ? 'animate-spin' : ''}`} />刷新</Button>
        </header>
        {error && <div className="heredity-execution-feedback heredity-execution-error" role="alert">
            <AlertCircle className="size-4 shrink-0" /><p>{error}</p></div>}
        {notice && <div className="heredity-execution-feedback" role="status">
            <CheckCircle2 className="size-4 shrink-0" /><p>{notice}</p></div>}
        {!payload && loading && <div className="heredity-execution-feedback" role="status">
            <RefreshCw className="size-4 animate-spin" /><p>正在读取执行状态</p></div>}
        {payload && <>
            <section className="heredity-execution-current" aria-labelledby="execution-current">
                <div className="heredity-execution-section-heading"><h3 id="execution-current">
                    <Activity className="size-4" />当前会话</h3>
                    <Badge variant="secondary">{managerStateLabel(payload.state.state)}</Badge></div>
                <dl className="heredity-execution-session">
                    <div><dt>活动提供方</dt><dd>{descriptor?.label ?? '无活动提供方'}</dd></div>
                    <div><dt>执行级别</dt><dd>{descriptor?.source_kind === 'demo'
                        ? 'DEMO · 不属于 L0–L3' : descriptor?.execution_level ?? '未分配'}</dd></div>
                    <div><dt>会话代次</dt><dd><code>{shortEpoch(payload.state.session_epoch)}</code></dd></div>
                    <div><dt>在途操作</dt><dd>{payload.state.in_flight}</dd></div>
                </dl>
                {descriptor && <p className="heredity-execution-provenance">
                    {provenanceLabel(descriptor.level_provenance)}
                    {descriptor.level_provenance === 'declared' && '；级别来自配置声明，尚未由本地实现确认。'}</p>}
                {descriptor && <Mechanism provider={descriptor} />}
                {switchingId && <p role="status" className="heredity-execution-channel-note">
                    切换请求处理中，等待管理端返回会话状态。</p>}
                {!switchingId && ['initializing', 'draining', 'switching'].includes(payload.state.state)
                    && <p role="status" className="heredity-execution-channel-note">
                        正在{payload.state.state === 'draining' ? '排空在途操作'
                            : payload.state.state === 'initializing' ? '初始化会话' : '切换会话'}，当前不能再次切换。</p>}
            </section>
            {!payload.canSwitch && <div className="heredity-execution-feedback" role="note">
                <Info className="size-4 shrink-0" /><p>管理通道当前不可用，不能切换提供方。此页面仍可查看实际执行状态。</p></div>}
            <section aria-label="四级执行边界" className="heredity-execution-levels">
                {payload.levels.map((level) => {
                    const providers = payload.state.providers.filter((provider) => provider.execution_level === level.level);
                    return <section key={level.level} className="heredity-execution-level">
                        <div className="heredity-execution-level-heading"><code className="heredity-execution-level-id">
                            {level.level}</code><div><h3>{level.label}</h3><p>{level.description}</p></div></div>
                        {providers.length ? providers.map((provider) => <Provider key={provider.provider_id}
                            provider={provider} payload={payload} busy={busy || !confirmed} switchingId={switchingId}
                            onSwitch={select} />)
                            : <p className="heredity-execution-empty">当前没有注册该级别的提供方。</p>}
                    </section>;
                })}
            </section>
            {unassigned.length > 0 && <section className="heredity-execution-other"><h3>未分配执行级别的提供方</h3>
                {unassigned.map((provider) => <Provider key={provider.provider_id} provider={provider}
                    payload={payload} busy={busy || !confirmed} switchingId={switchingId} onSwitch={select} />)}
            </section>}
        </>}
    </div>;
}
