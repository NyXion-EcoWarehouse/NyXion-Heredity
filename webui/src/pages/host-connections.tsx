import { useEffect, useRef, useState } from 'react';
import {
    Activity, Copy, Eye, EyeOff, KeyRound, Link2, Loader2, Plus, RefreshCw, Save,
    Server, Trash2, Webhook,
} from 'lucide-react';
import { message, post } from '../api';
import { useResource } from '../hooks';
import {
    hostConnectionDraft, hostConnectionPayload, parseHostConnections,
    type HostConnectionDraft, type SecretMode,
} from '../host-connections-model';
import { Badge, Button, Empty, Loading, Notice, PageHeading, Status } from '../ui';

function SecretField({ label, configured, mode, value, disabled, changeMode, changeValue }: {
    label: string; configured: boolean; mode: SecretMode; value: string; disabled: boolean;
    changeMode: (mode: SecretMode) => void; changeValue: (value: string) => void;
}) {
    const [visible, setVisible] = useState(false);
    useEffect(() => { if (mode !== 'replace') setVisible(false); }, [mode]);
    return <div className="host-secret-field">
        <div className="host-setting-label"><span>{label}</span>
            <Badge variant={configured ? 'success' : 'secondary'}>{configured ? '已配置' : '未配置'}</Badge></div>
        <select aria-label={`${label}操作`} disabled={disabled} value={mode}
            onChange={(event) => changeMode(event.target.value as SecretMode)}>
            <option value="keep">保持现有设置</option><option value="replace">设置新值</option>
            <option value="clear">清除现有设置</option>
        </select>
        {mode === 'replace' && <div className="password-field">
            <input aria-label={`新${label}`} type={visible ? 'text' : 'password'} value={value}
                autoComplete="new-password" maxLength={4096} disabled={disabled}
                onChange={(event) => changeValue(event.target.value)} />
            <Button type="button" size="icon" variant="ghost"
                title={visible ? `隐藏${label}` : `显示${label}`}
                aria-label={visible ? `隐藏${label}` : `显示${label}`}
                onClick={() => setVisible(!visible)}>
                {visible ? <EyeOff size={16} /> : <Eye size={16} />}
            </Button>
        </div>}
        {mode === 'clear' && <p className="error-text">{label}将在保存后清除。</p>}
    </div>;
}

export function HostConnectionsPage() {
    const resource = useResource('/api/heredity/host-connections', parseHostConnections);
    const [draft, setDraft] = useState<HostConnectionDraft | null>(null);
    const [dirty, setDirty] = useState(false);
    const [busy, setBusy] = useState(false);
    const [selectionBusy, setSelectionBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const mutation = useRef(false);
    useEffect(() => {
        if (resource.value && !dirty && !busy) setDraft(hostConnectionDraft(resource.value));
    }, [resource.value, dirty, busy]);
    useEffect(() => {
        const timer = setInterval(() => { void resource.refresh(); }, 5000);
        return () => clearInterval(timer);
    }, [resource.refresh]);
    const change = (patch: Partial<HostConnectionDraft>) => {
        setDraft((current) => current ? { ...current, ...patch } : current);
        setDirty(true); setNotice(null); setError(null);
    };
    const save = async (event: React.FormEvent) => {
        event.preventDefault();
        if (mutation.current || !draft) return;
        let payload;
        try { payload = hostConnectionPayload(draft); }
        catch (failure) { setError(message(failure)); return; }
        mutation.current = true; setBusy(true); setError(null); setNotice(null);
        try {
            const saved = parseHostConnections(await post('/api/heredity/host-connections', payload));
            resource.setValue(saved);
            setDraft(hostConnectionDraft(saved));
            setDirty(false);
            setNotice('连接设置已应用，平台会话无需重启。');
        } catch (failure) { setError(message(failure)); }
        finally { mutation.current = false; setBusy(false); }
    };
    const select = async (account: string) => {
        if (selectionBusy) return;
        setSelectionBusy(true); setError(null); setNotice(null);
        try {
            await post('/api/heredity/account-selection', { account: account || null });
            await resource.refresh();
        } catch (failure) { setError(message(failure)); }
        finally { setSelectionBusy(false); }
    };
    const copy = async (value: string) => {
        try { await navigator.clipboard.writeText(value); setNotice('连接地址已复制。'); }
        catch (failure) { setError(message(failure)); }
    };
    const settings = resource.value;
    const selection = settings?.account_selection;
    return <>
        <PageHeading title="连接" actions={<Button variant="outline" size="sm"
            disabled={resource.loading || busy} onClick={() => void resource.refresh()}>
            <RefreshCw size={14} className={resource.loading ? 'spin' : ''} />刷新
        </Button>} />
        {resource.error && <Notice error>宿主连接管理接口不可用：{resource.error}</Notice>}
        {error && <Notice error>{error}</Notice>}
        {notice && <Notice>{notice}</Notice>}
        {!settings && resource.loading ? <Loading text="正在读取宿主连接" /> : !settings ? null : <>
            <section className="section">
                <div className="section-heading"><h2><Server size={17} />OneBot HTTP</h2>
                    <Status value={settings.http.enabled} good="监听中" bad="未监听" /></div>
                <div className="host-endpoint-row">
                    <code>{settings.http.origin}</code>
                    <Button variant="ghost" size="icon" title="复制 OneBot HTTP 地址" aria-label="复制 OneBot HTTP 地址"
                        onClick={() => void copy(settings.http.origin)}><Copy size={15} /></Button>
                </div>
                <dl className="fact-grid host-connection-facts">
                    <div><dt>绑定地址</dt><dd><code>{settings.http.host}</code></dd></div>
                    <div><dt>端口</dt><dd><code>{settings.http.port}</code></dd></div>
                    <div><dt>接口鉴权</dt><dd><Status value={settings.http.authentication.configured}
                        good="已配置" bad="未配置" /></dd></div>
                    <div><dt>当前账号</dt><dd>{selection?.account ?? (
                        selection?.accounts.length === 1
                            ? `${selection.accounts[0].platform}:${selection.accounts[0].self_id}`
                            : '尚未选定')}</dd></div>
                </dl>
            </section>
            <section className="section">
                <div className="section-heading"><h2><Activity size={17} />账号路由</h2>
                    <span className="muted">{selection?.accounts.length ?? 0} 个在线账号</span></div>
                <label className="field host-account-select">OneBot 当前账号
                    <select value={selection?.account ?? ''} disabled={selectionBusy}
                        onChange={(event) => void select(event.target.value)}>
                        <option value="">自动选择唯一在线账号</option>
                        {selection?.account && !selection.accounts.some(
                            (account) => `${account.platform}:${account.self_id}` === selection.account)
                            && <option value={selection.account}>{selection.account} · 离线</option>}
                        {selection?.accounts.map((account) => <option
                            key={`${account.platform}:${account.self_id}`}
                            value={`${account.platform}:${account.self_id}`}>
                            {account.display_name ? `${account.display_name} · ` : ''}{account.platform}:{account.self_id}
                        </option>)}
                    </select>
                </label>
                {selection?.state === 'account_selection_required'
                    && <p className="section-note">有多个在线账号，请选择一个账号提供给此 OneBot 端点。</p>}
                {!selection?.accounts.length && <Empty text="暂无在线插件账号" />}
            </section>
            {draft && <form onSubmit={(event) => void save(event)} className="host-settings-form">
                <section className="section">
                    <div className="section-heading"><h2><KeyRound size={17} />接口鉴权</h2></div>
                    <SecretField label="接口令牌" configured={settings.http.authentication.configured}
                        mode={draft.accessTokenMode} value={draft.accessToken} disabled={busy}
                        changeMode={(mode) => change({ accessTokenMode: mode, accessToken: '' })}
                        changeValue={(value) => change({ accessToken: value })} />
                </section>
                <section className="section">
                    <div className="section-heading"><h2><Link2 size={17} />事件流</h2>
                        <Badge>SSE 扩展</Badge></div>
                    <label className="checkbox-label host-toggle">
                        <input type="checkbox" checked={draft.sseEnabled} disabled={busy}
                            onChange={(event) => change({ sseEnabled: event.target.checked })} />
                        启用 SSE
                    </label>
                    <div className="host-endpoint-row">
                        <code>{settings.sse.url}</code>
                        <Button type="button" variant="ghost" size="icon" title="复制 SSE 地址" aria-label="复制 SSE 地址"
                            onClick={() => void copy(settings.sse.url)}><Copy size={15} /></Button>
                    </div>
                </section>
                <section className="section">
                    <div className="section-heading"><h2><Webhook size={17} />HTTP 事件投递</h2>
                        <Button type="button" variant="ghost" size="icon" title="添加事件接收地址"
                            aria-label="添加事件接收地址" disabled={busy || draft.eventUrls.length >= 100}
                            onClick={() => change({ eventUrls: [...draft.eventUrls, ''] })}>
                            <Plus size={17} /></Button></div>
                    <div className="host-event-urls">
                        {draft.eventUrls.map((url, index) => <div className="host-event-url" key={index}>
                            <label className="field">事件接收地址 {index + 1}<input type="url" value={url}
                                placeholder="http://127.0.0.1:8080/onebot" disabled={busy}
                                autoComplete="off" onChange={(event) => change({
                                    eventUrls: draft.eventUrls.map((value, current) =>
                                        current === index ? event.target.value : value),
                                })} /></label>
                            <Button type="button" variant="ghost" size="icon" title={`移除事件接收地址 ${index + 1}`}
                                aria-label={`移除事件接收地址 ${index + 1}`} disabled={busy}
                                onClick={() => change({ eventUrls: draft.eventUrls.filter(
                                    (_, current) => current !== index) })}><Trash2 size={16} /></Button>
                        </div>)}
                        {!draft.eventUrls.length && <p className="section-note">未配置事件接收地址。</p>}
                    </div>
                    <p className="section-note">事件心跳间隔：{settings.event_delivery.heartbeat_seconds} 秒</p>
                    <SecretField label="事件签名密钥" configured={settings.event_delivery.signing_configured}
                        mode={draft.eventSecretMode} value={draft.eventSecret} disabled={busy}
                        changeMode={(mode) => change({ eventSecretMode: mode, eventSecret: '' })}
                        changeValue={(value) => change({ eventSecret: value })} />
                </section>
                <div className="host-settings-footer">
                    <span>设置对当前运行生效；重启后使用启动配置。</span>
                    <Button type="submit" disabled={busy || !dirty}>
                        {busy ? <Loader2 size={15} className="spin" /> : <Save size={15} />}保存连接设置
                    </Button>
                </div>
            </form>}
            <section className="section host-unsupported">
                <div className="section-heading"><h2>其他传输</h2></div>
                <div><span>WebSocket</span><Badge>尚未实现</Badge></div>
                <div><span>反向 WebSocket</span><Badge>尚未实现</Badge></div>
            </section>
        </>}
    </>;
}
