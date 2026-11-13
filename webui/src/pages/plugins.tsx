import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ArrowRight, CircleUserRound, Loader2, Play, Plug, RefreshCw, Square, Trash2 } from 'lucide-react';
import { api, message, post } from '../api';
import { useResource } from '../hooks';
import {
    configFields, configInputValues, configPayload, parsePluginCatalog, parsePluginChallenge, pluginInstancePath,
    PLUGIN_LABELS, SESSION_LABELS, type PluginChallenge, type PluginInstanceSnapshot,
} from '../plugin-model';
import { Badge, Button, Empty, Loading, Notice, PageHeading, Status } from '../ui';

function seenAt(value: number | null) {
    return value === null ? '尚未握手' : new Date(value * 1000).toLocaleTimeString();
}

function LoginChallenge({ plugin, instance = false }: { plugin: PluginInstanceSnapshot; instance?: boolean }) {
    const [challenge, setChallenge] = useState<PluginChallenge | null>(null);
    const [error, setError] = useState<string | null>(null);
    const active = ['starting', 'running'].includes(plugin.state) && plugin.session?.state !== 'online';
    useEffect(() => {
        setChallenge(null); setError(null);
        if (!active) return;
        let mounted = true;
        let timer: ReturnType<typeof setTimeout>;
        let controller: AbortController | null = null;
        const poll = async () => {
            controller = new AbortController();
            try {
                const value = parsePluginChallenge(await api(
                    `${instance ? pluginInstancePath(plugin.id, plugin.instance_key)
                        : `/api/heredity/plugins/${encodeURIComponent(plugin.id)}`}/challenge`,
                    { signal: controller.signal }));
                if (mounted) { setChallenge(value); setError(null); }
            } catch (failure) {
                if (mounted && !controller.signal.aborted) setError(message(failure));
            } finally {
                if (mounted) timer = setTimeout(() => void poll(), 2000);
            }
        };
        void poll();
        return () => { mounted = false; clearTimeout(timer); controller?.abort(); };
    }, [plugin.id, plugin.instance_key, instance, active]);
    if (error) return <Notice error>{error}</Notice>;
    if (!challenge) return null;
    const expired = challenge.expires_at !== null && challenge.expires_at * 1000 <= Date.now();
    return <div className="plugin-challenge">
        {challenge.image && !expired && <img src={challenge.image} alt={`${plugin.name} 登录二维码`}
            width={220} height={220} referrerPolicy="no-referrer" />}
        <div><h3>{challenge.title}</h3>
            {challenge.message && <p>{challenge.message}</p>}
            <p className={expired ? 'error-text' : 'muted'}>
                {expired ? '本次交互已过期' : challenge.kind === 'notice' ? '平台状态通知'
                    : challenge.expires_at === null ? '等待平台确认'
                    : `有效至 ${new Date(challenge.expires_at * 1000).toLocaleTimeString()}`}
            </p>
        </div>
    </div>;
}

export function PluginPanel({ plugin, hostRunning, refresh, instance = false, selected = false,
    selectAccount, remove }: {
    plugin: PluginInstanceSnapshot; hostRunning: boolean; refresh: () => Promise<void>;
    instance?: boolean; selected?: boolean; selectAccount?: () => Promise<void>;
    remove?: () => Promise<void>;
}) {
    const schema = useMemo(() => {
        try { return { fields: configFields(plugin.config_schema), error: null }; }
        catch (failure) { return { fields: [], error: message(failure) }; }
    }, [plugin.config_schema]);
    const savedValues = configInputValues(plugin.config_values);
    const [values, setValues] = useState<Record<string, string | boolean>>({});
    const [busy, setBusy] = useState<'start' | 'stop' | 'restart' | 'select' | 'remove' | null>(null);
    const [error, setError] = useState<string | null>(null);
    const mutation = useRef(false);
    const mounted = useRef(true);
    useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
    const active = ['handshaking', 'starting', 'running', 'stopping'].includes(plugin.state);
    const transitional = ['handshaking', 'stopping'].includes(plugin.state);
    const run = async (operation: 'start' | 'stop' | 'restart') => {
        if (mutation.current) return;
        mutation.current = true;
        setBusy(operation); setError(null);
        try {
            const payload = operation === 'start'
                ? { config: configPayload(schema.fields, { ...savedValues, ...values }) } : {};
            const path = instance || operation === 'restart' ? pluginInstancePath(plugin.id, plugin.instance_key)
                : `/api/heredity/plugins/${encodeURIComponent(plugin.id)}`;
            await post(`${path}/${operation}`, payload);
            if (mounted.current && operation === 'start') {
                setValues((current) => Object.fromEntries(Object.entries(current).filter(
                    ([key]) => !schema.fields.some((field) => field.key === key && field.secret))));
            }
            await refresh();
        } catch (failure) {
            if (mounted.current) setError(message(failure));
        } finally {
            mutation.current = false;
            if (mounted.current) setBusy(null);
        }
    };
    const localAction = async (operation: 'select' | 'remove', callback: () => Promise<void>) => {
        if (mutation.current) return;
        mutation.current = true;
        setBusy(operation); setError(null);
        try { await callback(); await refresh(); }
        catch (failure) { if (mounted.current) setError(message(failure)); }
        finally { mutation.current = false; if (mounted.current) setBusy(null); }
    };
    return <section className="section plugin-section">
        <div className="plugin-heading">
            <div className="plugin-name"><h2>{instance ? <CircleUserRound size={18} /> : <Plug size={18} />}
                {instance ? plugin.display_name : plugin.name}</h2>
                {(!instance || selected) && <div>{!instance && <>
                    <code>{plugin.id}</code><span>v{plugin.version}</span><Badge>{plugin.platform}</Badge></>}
                    {selected && <Badge variant="success">OneBot 当前账号</Badge>}
                </div>}</div>
            <div className="plugin-controls">
                <Badge variant={plugin.state === 'failed' ? 'destructive'
                    : plugin.state === 'running' ? 'success' : 'secondary'}>{PLUGIN_LABELS[plugin.state]}</Badge>
                {(plugin.instance_id !== null || plugin.has_saved_config) && <Button variant="outline" size="sm"
                    aria-label={`重启${plugin.display_name}`} title="使用已保存配置重启"
                    disabled={!!busy || transitional || !hostRunning} onClick={() => void run('restart')}>
                    <RefreshCw size={14} className={busy === 'restart' ? 'spin' : ''} />重启</Button>}
                {active ? <Button variant="outline" size="sm" disabled={!!busy || transitional || !hostRunning}
                    onClick={() => void run('stop')}>
                    {busy === 'stop' ? <Loader2 size={14} className="spin" /> : <Square size={14} />}停止
                </Button> : <Button size="sm" disabled={!!busy || !!schema.error || !hostRunning}
                    onClick={() => void run('start')}>
                    {busy === 'start' ? <Loader2 size={14} className="spin" /> : <Play size={14} />}启动
                </Button>}
                {instance && remove && !active && <Button variant="ghost" size="icon"
                    disabled={!!busy || !hostRunning} title="移除账号" aria-label={`移除${plugin.display_name}`}
                    onClick={() => void localAction('remove', remove)}>
                    {busy === 'remove' ? <Loader2 size={15} className="spin" /> : <Trash2 size={15} />}
                </Button>}
            </div>
        </div>
        {[error, plugin.error, schema.error].filter(Boolean).map((value, index) =>
            <Notice error key={`${index}:${value}`}>{value}</Notice>)}
        <dl className="fact-grid plugin-facts">
            <div><dt><Activity size={12} />宿主握手</dt><dd>
                <Status value={plugin.handshake.verified ? plugin.heartbeat.healthy : null}
                    unknown="尚未建立" good="正常" bad="已失联" />
            </dd></div>
            <div><dt>SDK / 执行方式</dt><dd>
                <code>{plugin.handshake.api_major}.{plugin.handshake.api_minor}</code> · 同进程
            </dd></div>
            <div><dt>最近握手 / 租期</dt><dd>{seenAt(plugin.heartbeat.last_seen_at)}
                <span className="muted"> · {plugin.heartbeat.lease_seconds} 秒</span>
                <small>序号 {plugin.heartbeat.sequence}</small></dd></div>
            <div><dt><CircleUserRound size={12} />平台账号</dt><dd>
                {plugin.session ? <>
                    <Status value={plugin.session.state === 'online'}
                        good="在线" bad={SESSION_LABELS[plugin.session.state]} />
                    {plugin.session.self_id && <small><code>{plugin.session.self_id}</code></small>}
                </> : '尚未创建会话'}
            </dd></div>
        </dl>
        {plugin.session?.reason && <p className="section-note">{plugin.session.reason}</p>}
        {instance && plugin.session?.state === 'online' && selectAccount && !selected &&
            <div className="plugin-account-routing"><Button variant="outline" size="sm" disabled={!!busy}
                onClick={() => void localAction('select', selectAccount)}>
                {busy === 'select' ? <Loader2 size={14} className="spin" /> : <ArrowRight size={14} />}
                设为 OneBot 当前账号
            </Button></div>}
        <details className="plugin-config" open={!active}>
            <summary>启动配置</summary>
            <div className="plugin-fields">
                {schema.fields.map((field) => <label key={field.key}
                    className={field.type === 'boolean' ? 'plugin-checkbox' : 'field'}>
                    <span>{field.title}{field.required && <span className="required-mark"> *</span>}</span>
                    {field.type === 'boolean' ? <input type="checkbox" disabled={active || !!busy}
                        checked={(values[field.key] ?? savedValues[field.key] ?? field.defaultValue) === true}
                        onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.checked }))} />
                        : field.choices.length ? <select disabled={active || !!busy}
                            value={String(values[field.key] ?? savedValues[field.key] ?? field.defaultValue)}
                            onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))}>
                            {!field.required && <option value="">未设置</option>}
                            {field.required && !field.defaultValue && <option value="">请选择</option>}
                            {field.choices.map((choice) => <option key={String(choice)} value={String(choice)}>{String(choice)}</option>)}
                        </select> : <input disabled={active || !!busy}
                            type={field.secret ? 'password' : field.type === 'string'
                                || /(?:self|user|account)_id$/.test(field.key) ? 'text' : 'number'}
                            inputMode={field.type === 'integer' && /(?:self|user|account)_id$/.test(field.key)
                                ? 'numeric' : undefined}
                            autoComplete={field.secret ? 'new-password' : 'off'}
                            step={field.type === 'integer' ? 1 : 'any'} min={field.minimum} max={field.maximum}
                            required={field.required} value={String(values[field.key] ?? savedValues[field.key] ?? field.defaultValue)}
                            onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))} />}
                    {field.description && <small>{field.description}</small>}
                </label>)}
            </div>
            {!schema.fields.length && !schema.error && <p className="section-note">此插件无需启动配置。</p>}
        </details>
        <LoginChallenge plugin={plugin} instance={instance} />
        <details className="plugin-capabilities">
            <summary>能力与动作 <span>{plugin.actions.length}</span></summary>
            <div>{plugin.capabilities.map((capability) => <Badge key={capability}>{capability}</Badge>)}</div>
            <div>{plugin.actions.map((action) => <code key={action}>{action}</code>)}</div>
        </details>
    </section>;
}

export function PluginsPage({ navigate }: { navigate: (path: string) => void }) {
    const resource = useResource('/api/heredity/plugins', parsePluginCatalog);
    useEffect(() => {
        const timer = setInterval(() => { void resource.refresh(); }, 3000);
        return () => clearInterval(timer);
    }, [resource.refresh]);
    return <>
        <PageHeading title="插件" actions={<Button variant="outline" size="sm"
            disabled={resource.loading} onClick={() => void resource.refresh()}>
            <RefreshCw size={14} className={resource.loading ? 'spin' : ''} />刷新
        </Button>} />
        {resource.error && <Notice error>{resource.error}</Notice>}
        {resource.value?.host_running === false && <Notice>插件宿主尚未运行。</Notice>}
        {resource.value && <div className="plugin-host-line">
            <Status value={resource.value.host_running} good="插件宿主运行中" bad="插件宿主未运行" />
            <span>SDK {resource.value.api_version}</span>
            <span>{resource.value.plugins.length} 个插件</span>
            <span>{resource.value.sessions.filter((session) => session.state === 'online').length} 个在线账号</span>
        </div>}
        {!resource.value && resource.loading ? <Loading text="正在读取插件" />
            : resource.value?.plugins.length === 0 && <Empty text="没有已注册的插件" />}
        {resource.value?.plugins.map((plugin) => plugin.ui_pages.length ? <section key={plugin.id}
            className="section plugin-section">
            <div className="plugin-heading">
                <div className="plugin-name"><h2><Plug size={18} />{plugin.name}</h2>
                    <div><code>{plugin.id}</code><span>v{plugin.version}</span><Badge>{plugin.platform}</Badge></div></div>
                <div className="plugin-controls"><Badge>{plugin.instances.length} 个账号位</Badge>
                    <Badge variant={plugin.instances.some((item) => item.session?.state === 'online') ? 'success' : 'secondary'}>
                        {plugin.instances.filter((item) => item.session?.state === 'online').length} 在线
                    </Badge></div>
            </div>
            <div className="plugin-page-links">{plugin.ui_pages.map((page) => <Button key={page.id}
                variant="outline" size="sm" onClick={() => navigate(page.path)}>
                <CircleUserRound size={15} />{page.title}<ArrowRight size={14} />
            </Button>)}</div>
            <div className="plugin-capabilities"><div>{plugin.capabilities.map((capability) =>
                <Badge key={capability}>{capability}</Badge>)}</div></div>
        </section> : <PluginPanel key={plugin.id} plugin={plugin}
            hostRunning={resource.value!.host_running} refresh={resource.refresh} />)}
    </>;
}
