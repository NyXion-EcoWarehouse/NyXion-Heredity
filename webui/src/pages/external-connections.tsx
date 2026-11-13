import { useEffect, useRef, useState } from 'react';
import { Check, CircleUserRound, Copy, Eye, Loader2, Plug, RefreshCw, Search, Unplug, X } from 'lucide-react';
import { api, asObject, message, post } from '../api';
import { useResource } from '../hooks';
import { localOrigin, parseConnection, parseProbe, type ConnectionProbe } from '../models';
import { Button, Notice, PageHeading, Status } from '../ui';

export function ExternalConnectionsPage() {
    const connection = useResource('/api/heredity/connection', parseConnection);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const [origin, setOrigin] = useState('http://127.0.0.1:3001');
    const [token, setToken] = useState('');
    const [level, setLevel] = useState('');
    const [probe, setProbe] = useState<ConnectionProbe | null>(null);
    const [gateway, setGateway] = useState<{ origin: string; access_token: string } | null>(null);
    const [eventConfig, setEventConfig] = useState<{ url: string; secret: string } | null>(null);
    const [copied, setCopied] = useState(false);
    const mutation = useRef(false);
    const mounted = useRef(true);
    useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
    const active = connection.value?.active;
    const hasGateway = Boolean(active?.gateway_origin ?? active?.origin);
    const run = async (name: string, operation: () => Promise<void>) => {
        if (mutation.current) return;
        mutation.current = true;
        setBusy(name); setError(null); setNotice(null);
        try { await operation(); }
        catch (failure) { if (mounted.current) setError(message(failure)); }
        finally {
            mutation.current = false;
            if (mounted.current) setBusy(null);
        }
    };
    const probeOrigin = () => void run('probe', async () => {
        setProbe(null);
        const value = parseProbe(await post('/api/heredity/connection/probe', {
            origin: localOrigin(origin), ...(token ? { access_token: token } : {}),
            execution_level: level || null,
        }));
        setProbe(value); setOrigin(value.origin);
    });
    const connect = () => void run('connect', async () => {
        if (!probe) throw new Error('请先检测接口并确认账号');
        if (Date.now() / 1000 >= probe.expires_at) {
            setProbe(null);
            throw new Error('接口检测已过期，请重新检测');
        }
        connection.setValue(parseConnection(await post('/api/heredity/connection/connect', {
            probe_id: probe.probe_id,
        })));
        setProbe(null); setToken(''); setGateway(null); setEventConfig(null);
        setNotice('已接通检测确认的账号。');
    });
    const disconnect = () => void run('disconnect', async () => {
        connection.setValue(parseConnection(await post('/api/heredity/connection/disconnect', {})));
        setProbe(null); setGateway(null); setEventConfig(null);
        setNotice('已恢复默认网关。');
    });
    const gatewayConfig = () => void run('gateway', async () => {
        const value = asObject(await api('/api/heredity/connection/gateway'), '网关配置');
        if (typeof value.origin !== 'string' || typeof value.access_token !== 'string') {
            throw new Error('网关配置格式无效');
        }
        setGateway({ origin: value.origin, access_token: value.access_token });
    });
    const loadEventConfig = () => void run('eventConfig', async () => {
        const value = asObject(await api('/api/heredity/connection/events/config'), '事件回调配置');
        if (typeof value.url !== 'string' || typeof value.secret !== 'string') {
            throw new Error('事件回调配置格式无效');
        }
        setEventConfig({ url: value.url, secret: value.secret });
    });
    const inputChanged = () => setProbe(null);

    return <>
        <PageHeading title="外部 OneBot" subtitle="检测本机服务并确认活动账号"
            actions={<Button variant="outline" size="sm" disabled={!!busy || connection.loading}
                onClick={() => void connection.refresh()}>
                <RefreshCw size={14} className={connection.loading ? 'spin' : ''} />刷新状态
            </Button>} />
        {error && <Notice error>{error}</Notice>}
        {notice && <Notice>{notice}</Notice>}
        <section className="section">
            <div className="section-heading"><h2><CircleUserRound size={17} />账号连接</h2></div>
            {connection.error && <Notice error>{connection.error}</Notice>}
            {connection.value?.error && <Notice error>{connection.value.error}</Notice>}
            <div className="connection-line">
                <div className="connection-identity"><div>
                    <strong>{active?.verified ? active.nickname || active.self_id : '账号未验证'}</strong>
                    {active?.verified && <code>{active.self_id}</code>}</div>
                    <code>{active?.origin ?? (active?.kind === 'gateway' ? '本地插件通道' : '无活动连接')}</code></div>
                <Status value={active?.verified ? active.online : null} />
                {active?.kind === 'external' && <Button variant="outline" size="sm"
                    disabled={!!busy} onClick={disconnect}><Unplug size={14} />恢复默认网关</Button>}
            </div>
            {connection.value?.event_ingress?.enabled === false && <Notice>
                入站事件未接入。{connection.value.event_ingress.reason}
            </Notice>}
            {connection.value?.event_ingress?.enabled === true && <div className="event-ingress-status">
                <Status value={connection.value.event_ingress.healthy}
                    unknown="等待上游事件" good="已收到上游事件" bad="事件接收异常" />
                <span>{connection.value.event_ingress.accepted} 条已接收 · {connection.value.event_ingress.duplicates} 条重复</span>
                {connection.value.event_ingress.last_event_at !== null
                    && <time>最近 {new Date(connection.value.event_ingress.last_event_at * 1000).toLocaleTimeString('zh-CN')}</time>}
            </div>}
            <div className="connect-form">
                <label className="field field-wide">本机接口地址
                    <input value={origin} placeholder="http://127.0.0.1:3001" spellCheck={false}
                        onChange={(event) => { setOrigin(event.target.value); inputChanged(); }} disabled={!!busy} /></label>
                <label className="field">接口令牌
                    <input type="password" value={token} placeholder="可选" autoComplete="off"
                        onChange={(event) => { setToken(event.target.value); inputChanged(); }} disabled={!!busy} /></label>
                <label className="field">声明执行级别
                    <select aria-label="声明执行级别" value={level} disabled={!!busy}
                        onChange={(event) => { setLevel(event.target.value); inputChanged(); }}>
                        <option value="">未声明</option>
                        <option value="L0">L0 · 界面驱动</option>
                        <option value="L1">L1 · 进程附着</option>
                        <option value="L2">L2 · 完整客户端</option>
                        <option value="L3">L3 · 协议核心</option>
                    </select></label>
                <Button variant="outline" disabled={!!busy || !origin.trim()} onClick={probeOrigin}>
                    {busy === 'probe' ? <Loader2 size={15} className="spin" /> : <Search size={15} />}检测接口
                </Button>
            </div>
            {level && <p className="section-note">级别来自外部服务声明，不代表本地实现已验证。</p>}
            {probe && <div className="probe-result">
                <div><Check size={17} /><strong>{probe.nickname || '检测到账号'}</strong>
                    <code>{probe.self_id}</code><Status value={probe.online} good="在线" bad="离线" /></div>
                <p>{probe.origin}</p>
                <small className="muted">检测有效至 {new Date(probe.expires_at * 1000).toLocaleTimeString('zh-CN')}</small>
                <Button disabled={!!busy} onClick={connect}>
                    {busy === 'connect' ? <Loader2 size={15} className="spin" /> : <Plug size={15} />}接通此账号
                </Button>
            </div>}
            <div className="gateway-controls">
                <Button variant="ghost" size="sm" disabled={!!busy || !hasGateway} onClick={gatewayConfig}>
                    <Eye size={14} />查看网关配置</Button>
                {gateway && <Button variant="ghost" size="sm" onClick={() => setGateway(null)}><X size={14} />收起</Button>}
                <Button variant="ghost" size="sm" disabled={!!busy || active?.kind !== 'external'} onClick={loadEventConfig}>
                    <Eye size={14} />事件回调配置</Button>
                {eventConfig && <Button variant="ghost" size="sm" onClick={() => setEventConfig(null)}><X size={14} />收起回调</Button>}
            </div>
            {gateway && <div className="gateway-config">
                <label className="field">OneBot 网关地址<input readOnly value={gateway.origin} /></label>
                <label className="field">网关令牌<input type="password" readOnly value={gateway.access_token} /></label>
                <Button variant="outline" size="sm" onClick={() => void run('copy', async () => {
                    await navigator.clipboard.writeText(JSON.stringify(gateway, null, 2));
                    setCopied(true); setTimeout(() => setCopied(false), 2000);
                })}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? '已复制' : '复制配置'}</Button>
            </div>}
            {eventConfig && <div className="gateway-config">
                <label className="field">事件回调地址<input readOnly value={eventConfig.url} /></label>
                <label className="field">签名密钥<input type="password" readOnly value={eventConfig.secret} /></label>
                <Button variant="outline" size="sm" onClick={() => void run('copyEvent', async () => {
                    await navigator.clipboard.writeText(JSON.stringify(eventConfig, null, 2));
                    setNotice('事件回调配置已复制。');
                })}><Copy size={14} />复制回调配置</Button>
            </div>}
        </section>
    </>;
}
