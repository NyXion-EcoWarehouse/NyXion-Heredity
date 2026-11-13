import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import {
    Activity, Check, Copy, Eye, EyeOff, KeyRound, Link2, Loader2, Network, Pencil, Play,
    Plus, RefreshCw, RotateCcw, Save, Server, Square, Trash2, Unlink, X,
} from 'lucide-react';
import { api, message, post } from '../api';
import { useResource } from '../hooks';
import {
    interfaceDraft, interfacePath, interfacePayload, INTERFACE_STATE_LABELS,
    isServerType, parseInterfaceCredentials, parseNetworkInterface, parseNetworkInterfaces,
    parseRegeneratedCredentials,
    type InterfaceDraft, type InterfaceType, type NetworkInterface, type NetworkInterfaceCatalog,
} from '../network-interfaces-model';
import {
    assignableInterfaces, assignmentPayload, canAssignRequest, currentNetworkRequests,
    historyNetworkRequests, networkRequestPath, parseNetworkRequest, parseNetworkRequests,
    REQUEST_STATE_LABELS, sameInterfaceSelection,
    type NetworkRequest,
} from '../network-requests-model';
import { Badge, Button, Empty, Loading, Notice, PageHeading, Status } from '../ui';

function InterfaceEditor({ source, catalog, close, saved }: {
    source: NetworkInterface | null; catalog: NetworkInterfaceCatalog;
    close: () => void; saved: () => Promise<void>;
}) {
    const [draft, setDraft] = useState<InterfaceDraft>(() => interfaceDraft(source ?? undefined));
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const guard = useRef(false);
    const dialog = useRef<HTMLElement | null>(null);
    const creating = source === null;
    const selectedType = catalog.types.find((entry) => entry.type === draft.type);
    const formId = creating ? 'network-interface-create' : `network-interface-${source.interface_id}`;
    const change = (patch: Partial<InterfaceDraft>) => {
        setDraft((current) => ({ ...current, ...patch })); setError(null);
    };
    useEffect(() => {
        const previous = document.activeElement as HTMLElement | null;
        dialog.current?.querySelector<HTMLInputElement>('input')?.focus();
        return () => previous?.focus();
    }, []);
    const keyboard = (event: KeyboardEvent<HTMLElement>) => {
        if (event.key === 'Escape' && !busy) { event.preventDefault(); close(); }
        if (event.key !== 'Tab') return;
        const controls = [...(dialog.current?.querySelectorAll<HTMLElement>(
            'input:not(:disabled),button:not(:disabled),select:not(:disabled)') ?? [])];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    const save = async (event: React.FormEvent) => {
        event.preventDefault();
        if (guard.current) return;
        let payload;
        try {
            if (!selectedType?.implemented) throw new Error('所选接口类型尚不可用');
            payload = interfacePayload(draft, creating);
        } catch (failure) { setError(message(failure)); return; }
        guard.current = true; setBusy(true); setError(null);
        try {
            const value = creating
                ? await post('/api/heredity/network-interfaces', payload)
                : await api(interfacePath(source.interface_id), {
                    method: 'PUT', body: JSON.stringify(payload),
                });
            parseNetworkInterface(value);
            await saved();
            close();
        } catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(false); }
    };
    return <div className="modal-backdrop" onClick={(event) => {
        if (event.target === event.currentTarget && !busy) close();
    }}>
        <section className="dialog network-dialog" role="dialog" aria-modal="true"
            aria-labelledby={`${formId}-title`} ref={dialog} onKeyDown={keyboard}>
            <div className="section-heading network-dialog-heading">
                <h2 id={`${formId}-title`}>{creating ? '新建网络接口' : '编辑网络接口'}</h2>
                <Button type="button" variant="ghost" size="icon" aria-label="关闭接口配置" title="关闭"
                    disabled={busy} onClick={close}><X size={18} /></Button>
            </div>
            {!creating && <div className="network-editor-id"><span>接口 ID</span><code>{source.interface_id}</code></div>}
            {error && <Notice error>{error}</Notice>}
            <form id={formId} onSubmit={(event) => void save(event)} className="network-editor-form">
                <label className="field">接口名称<input value={draft.name} maxLength={80} required
                    disabled={busy} onChange={(event) => change({ name: event.target.value })}
                    placeholder="例如：机器人 HTTP 接口" /></label>
                <fieldset className="network-types" disabled={busy || !creating}>
                    <legend>接口类型</legend>
                    <div role="radiogroup" aria-label="接口类型">
                        {catalog.types.map((entry) => <label key={entry.type}
                            className={`network-type ${draft.type === entry.type ? 'selected' : ''}`}>
                            <input type="radio" name={`${formId}-type`} value={entry.type}
                                disabled={!entry.implemented} checked={draft.type === entry.type}
                                onChange={() => change({ type: entry.type as InterfaceType })} />
                            <span>{entry.label}</span>{!entry.implemented && <small>未实现</small>}
                        </label>)}
                    </div>
                    {selectedType?.description && <p>{selectedType.description}</p>}
                </fieldset>
                <div className="network-editor-grid">
                    {isServerType(draft.type) ? <>
                        <label className="field">监听地址<input value={draft.host} disabled={busy}
                            required autoComplete="off"
                            onChange={(event) => change({ host: event.target.value })} /></label>
                        <label className="field">监听端口<input value={draft.port} type="number"
                            min={0} max={65535} step={1} required disabled={busy}
                            onChange={(event) => change({ port: event.target.value })} />
                            <small>0 表示启动时分配可用端口。</small></label>
                        <label className="field network-field-full">接口路径<input value={draft.path}
                            disabled={busy} required autoComplete="off"
                            onChange={(event) => change({ path: event.target.value })} /></label>
                    </> : <label className="field network-field-full">远端地址<input value={draft.url}
                        disabled={busy} required autoComplete="off"
                        placeholder={draft.type === 'http_client' ? 'http://127.0.0.1:8080/onebot' : 'ws://127.0.0.1:8080/onebot'}
                        onChange={(event) => change({ url: event.target.value })} /></label>}
                    <label className="field">绑定账号<input value={draft.account} disabled={busy}
                        list={`${formId}-accounts`} placeholder="留空自动；格式：平台:账号" autoComplete="off"
                        onChange={(event) => change({ account: event.target.value })} />
                        <datalist id={`${formId}-accounts`}>
                        {catalog.accounts.map((entry) => {
                            const identity = `${entry.platform}:${entry.self_id}`;
                            return <option key={identity} value={identity}>{entry.display_name ?? identity}</option>;
                        })}
                        </datalist><small>留空时自动选择唯一在线账号，也可填写离线账号。</small>
                    </label>
                    <label className="field">心跳间隔（秒）<input type="number" value={draft.heartbeatSeconds}
                        min={1} max={86400} step="any" required disabled={busy}
                        onChange={(event) => change({ heartbeatSeconds: event.target.value })} /></label>
                </div>
                {draft.type === 'http_server' && <label className="checkbox-label network-sse">
                    <input type="checkbox" checked={draft.sseEnabled} disabled={busy}
                        onChange={(event) => change({ sseEnabled: event.target.checked })} />启用 SSE 事件扩展
                </label>}
                <div className="network-editor-footer">
                    <span>{creating ? '创建后为停止状态，ID 与令牌由宿主生成。'
                        : source.enabled ? '保存后重新建立连接，接口 ID 保持不变。' : '保存后保持停止状态。'}</span>
                    <div><Button type="button" variant="ghost" disabled={busy} onClick={close}>取消</Button>
                        <Button type="submit" disabled={busy || !selectedType?.implemented}>
                            {busy ? <Loader2 size={15} className="spin" /> : creating ? <Plus size={15} /> : <Save size={15} />}
                            {creating ? '创建接口' : '保存配置'}
                        </Button></div>
                </div>
            </form>
        </section>
    </div>;
}

function InterfaceCredentials({ item, copied, refresh }: {
    item: NetworkInterface; copied: (value: string, label: string) => Promise<void>;
    refresh: () => Promise<void>;
}) {
    const [open, setOpen] = useState(false);
    const [token, setToken] = useState<string | null>(null);
    const [visible, setVisible] = useState(false);
    const [busy, setBusy] = useState(false);
    const [confirm, setConfirm] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const guard = useRef(false);
    const load = async () => {
        if (guard.current) return;
        if (open) { setOpen(false); setToken(null); setVisible(false); setConfirm(false); return; }
        guard.current = true; setBusy(true); setError(null); setOpen(true);
        try {
            const credentials = parseInterfaceCredentials(
                await api(`${interfacePath(item.interface_id)}/credentials`, { cache: 'no-store' }),
                item.interface_id);
            setToken(credentials.access_token);
        } catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(false); }
    };
    const regenerate = async () => {
        if (guard.current) return;
        guard.current = true; setBusy(true); setError(null);
        try {
            const credentials = parseRegeneratedCredentials(
                await post(`${interfacePath(item.interface_id)}/regenerate-token`, {}), item.interface_id);
            setToken(credentials.access_token); setVisible(false); setConfirm(false);
            await refresh();
        } catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(false); }
    };
    return <div className="network-credentials">
        <Button variant="ghost" size="sm" disabled={busy} aria-expanded={open}
            aria-label={`${open ? '收起' : '查看'} ${item.name} 的接口令牌`}
            onClick={() => void load()}><KeyRound size={14} />接口令牌</Button>
        {open && <div className="network-credentials-body">
            {busy && !token && <Loading text="正在读取接口令牌" />}
            {error && <Notice error>{error}</Notice>}
            {token && <>
                <label className="field">接口令牌
                    <div className="network-token-field password-field">
                        <input readOnly autoComplete="off" spellCheck={false}
                            aria-label={`${item.name} 的接口令牌`} value={token}
                            type={visible ? 'text' : 'password'} />
                        <Button variant="ghost" size="icon" title={visible ? '隐藏令牌' : '显示令牌'}
                            aria-label={`${visible ? '隐藏' : '显示'} ${item.name} 的接口令牌`}
                            onClick={() => setVisible(!visible)}>
                            {visible ? <EyeOff size={15} /> : <Eye size={15} />}</Button>
                        <Button variant="ghost" size="icon" title="复制令牌"
                            aria-label={`复制 ${item.name} 的接口令牌`} onClick={() => void copied(token, '接口令牌')}>
                            <Copy size={15} /></Button>
                    </div>
                </label>
                <div className="network-token-footer"><span>事件签名使用相同令牌。</span>
                    <Button variant="outline" size="sm" disabled={busy}
                        onClick={() => setConfirm(true)}><RefreshCw size={13} />重新生成</Button></div>
                {confirm && <div className="network-delete-confirm" role="alert">
                    <span>重新生成后，已连接的客户端需更新接口令牌。</span>
                    <div><Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirm(false)}>取消</Button>
                        <Button variant="danger" size="sm" disabled={busy} onClick={() => void regenerate()}>
                            {busy ? <Loader2 size={14} className="spin" /> : <KeyRound size={14} />}确认重新生成
                        </Button></div>
                </div>}
            </>}
        </div>}
    </div>;
}

function InterfaceRow({ item, catalog, edit, refresh, copied }: {
    item: NetworkInterface; catalog: NetworkInterfaceCatalog; edit: () => void;
    refresh: () => Promise<void>; copied: (value: string, label: string) => Promise<void>;
}) {
    const [busy, setBusy] = useState<'start' | 'stop' | 'retry' | 'delete' | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const guard = useRef(false);
    const active = item.enabled || item.state === 'running' || item.state === 'starting';
    const definition = catalog.types.find((entry) => entry.type === item.type);
    const act = async (operation: 'start' | 'stop' | 'retry' | 'delete') => {
        if (guard.current) return;
        guard.current = true; setBusy(operation); setError(null);
        try {
            if (operation === 'delete') await api(interfacePath(item.interface_id), { method: 'DELETE' });
            else parseNetworkInterface(await post(`${interfacePath(item.interface_id)}/${operation}`, {}));
            setConfirmDelete(false);
            await refresh();
        } catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(null); }
    };
    return <section className="section network-interface-section" data-interface-id={item.interface_id}>
        <div className="network-interface-heading">
            <div className="network-interface-name">
                <h2>{isServerType(item.type) ? <Server size={17} /> : <Link2 size={17} />}{item.name}</h2>
                <div className="network-interface-identity"><code>{item.interface_id}</code>
                    <Button variant="ghost" size="icon" title="复制接口 ID" aria-label={`复制 ${item.name} 的接口 ID`}
                        onClick={() => void copied(item.interface_id, '接口 ID')}><Copy size={14} /></Button>
                    <Badge>{definition?.label ?? item.type}</Badge>
                    {item.is_primary && <Badge variant="outline">主接口</Badge>}
                </div>
            </div>
            <div className="network-interface-actions">
                <Badge variant={item.state === 'error' ? 'destructive'
                    : item.state === 'running' ? 'success' : 'secondary'}>{INTERFACE_STATE_LABELS[item.state]}</Badge>
                <Button variant="ghost" size="icon" aria-label={`编辑 ${item.name}`} title="编辑配置"
                    disabled={!!busy || item.state === 'starting'} onClick={edit}><Pencil size={15} /></Button>
                {(active || item.state === 'error') && <Button variant="ghost" size="icon"
                    aria-label={`重连 ${item.name}`} title="重新连接" disabled={!!busy || item.state === 'starting'}
                    onClick={() => void act('retry')}>
                    <RefreshCw size={15} className={busy === 'retry' ? 'spin' : ''} /></Button>}
                {active ? <Button variant="outline" size="sm" disabled={!!busy || item.state === 'starting'}
                    onClick={() => void act('stop')}>
                    {busy === 'stop' ? <Loader2 size={14} className="spin" /> : <Square size={14} />}停止
                </Button> : <Button size="sm" disabled={!!busy || !definition?.implemented}
                    onClick={() => void act('start')}>
                    {busy === 'start' ? <Loader2 size={14} className="spin" /> : <Play size={14} />}启动
                </Button>}
                {!item.is_primary && <Button variant="ghost" size="icon" title={active ? '停止接口后删除' : '删除接口'}
                    aria-label={`删除 ${item.name}`} disabled={!!busy || active} onClick={() => setConfirmDelete(true)}>
                    <Trash2 size={15} /></Button>}
            </div>
        </div>
        {[error, item.error].filter(Boolean).map((value, index) => <Notice error key={`${index}:${value}`}>{value}</Notice>)}
        {confirmDelete && <div className="network-delete-confirm" role="alert">
            <span>删除接口“{item.name}”？模块将无法再通过此 ID 绑定。</span>
            <div><Button variant="ghost" size="sm" disabled={!!busy} onClick={() => setConfirmDelete(false)}>取消</Button>
                <Button variant="danger" size="sm" disabled={!!busy} onClick={() => void act('delete')}>
                    {busy === 'delete' ? <Loader2 size={14} className="spin" /> : <Trash2 size={14} />}确认删除
                </Button></div>
        </div>}
        <div className="network-interface-endpoint">
            <span>端点</span><code>{item.endpoint ?? (isServerType(item.type)
                ? `${item.settings.host ?? ''}:${item.settings.port ?? 0}${item.settings.path ?? '/'}`
                : item.settings.url ?? '未配置')}</code>
            {item.endpoint && <Button variant="ghost" size="icon" title="复制端点" aria-label={`复制 ${item.name} 的端点`}
                onClick={() => void copied(item.endpoint!, '端点')}><Copy size={14} /></Button>}
        </div>
        <dl className="fact-grid network-interface-facts">
            <div><dt>绑定账号</dt><dd><code>{item.account ?? '自动选择唯一在线账号'}</code></dd></div>
            <div><dt>接口鉴权</dt><dd><Status value={item.settings.authentication.configured} good="已配置" bad="未配置" /></dd></div>
            <div><dt>心跳间隔</dt><dd>{item.settings.heartbeat_seconds} 秒</dd></div>
            <div><dt>模块绑定</dt><dd>{item.modules.length} 个模块 · {item.modules.reduce(
                (total, entry) => total + entry.subscriptions, 0)} 个订阅</dd></div>
        </dl>
        <InterfaceCredentials item={item} copied={copied} refresh={refresh} />
        {item.modules.length > 0 && <details className="network-module-list"><summary>已绑定模块</summary>
            <div>{item.modules.map((entry) => <span key={entry.module_id}><code>{entry.module_id}</code>
                <small>{entry.subscriptions} 个订阅</small></span>)}</div>
        </details>}
    </section>;
}

function ModuleRequest({ item, catalog, refresh, historical = false, reapplied }: {
    item: NetworkRequest; catalog: NetworkInterfaceCatalog; refresh: () => Promise<void>;
    historical?: boolean; reapplied?: (requestId: string) => void;
}) {
    const available = assignableInterfaces(item, catalog.interfaces);
    const assigned = item.grants.map((grant) => grant.interface_id);
    const [draft, setDraft] = useState<string[] | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [pendingAutoStart, setPendingAutoStart] = useState<boolean | null>(null);
    const [error, setError] = useState<string | null>(null);
    const guard = useRef(false);
    const selected = draft ?? assigned;
    const dirty = !sameInterfaceSelection(selected, assigned);
    const invalid = selected.some((id) => !available.some((entry) => entry.interface_id === id));
    const editable = canAssignRequest(item) && !historical;
    const updating = !!busy || item.restoring;
    const assignmentVersion = [...assigned].sort().join(',');
    useEffect(() => {
        if (draft !== null && sameInterfaceSelection(draft, assigned)) setDraft(null);
    }, [assignmentVersion, draft]);
    const toggle = (id: string, checked: boolean) => {
        const next = checked ? [...selected, id] : selected.filter((value) => value !== id);
        setDraft(sameInterfaceSelection(next, assigned) ? null : next);
        setError(null);
    };
    const act = async (operation: 'assign' | 'unassign' | 'auto-start' | 'reapply',
        interfaceId?: string, enabled?: boolean) => {
        if (guard.current || (operation === 'assign' && invalid)) return;
        let payload: Record<string, unknown> = {};
        try {
            if (operation === 'assign') payload = assignmentPayload(item, selected);
            if (operation === 'unassign' && interfaceId) payload = { interface_id: interfaceId };
            if (operation === 'auto-start') payload = { enabled };
        } catch (failure) { setError(message(failure)); return; }
        guard.current = true; setBusy(interfaceId ?? operation); setError(null);
        if (operation === 'auto-start') setPendingAutoStart(enabled ?? false);
        try {
            const updated = parseNetworkRequest(await post(`${networkRequestPath(item.request_id)}/${operation}`, payload));
            await refresh();
            if (operation === 'unassign' && interfaceId) {
                setDraft((current) => current?.filter((id) => id !== interfaceId) ?? null);
            } else if (operation !== 'auto-start') setDraft(null);
            if (operation === 'reapply') reapplied?.(updated.request_id);
        } catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(null); setPendingAutoStart(null); }
    };
    const directions = { call: '调用动作', publish: '发布事件', receive: '接收消息' };
    return <div className="network-request" data-network-request-id={item.request_id}>
        <div className="network-request-heading"><code>{item.module_id}</code>
            <div>{item.restoring && <Badge><Loader2 size={11} className="spin" />恢复接口中</Badge>}
                {!item.module_active && <Badge>模块未运行</Badge>}
                <Badge variant={item.state === 'granted' ? 'success'
                    : item.state === 'pending' ? 'warning' : 'secondary'}>{REQUEST_STATE_LABELS[item.state]}</Badge></div></div>
        {item.requirement.purpose && <p>{item.requirement.purpose}</p>}
        <div className="network-request-meta">
            <span>{item.requirement.transport_types.map((kind) => catalog.types.find(
                (entry) => entry.type === kind)?.label ?? kind).join(' / ')}</span>
            <span>{item.requirement.directions.map((direction) => directions[direction]).join(' · ')}</span>
            {item.requirement.account && <code>{item.requirement.account}</code>}
            <span>最多 {item.requirement.max_interfaces} 个接口</span>
        </div>
        <div className="network-request-policy">
            <label className="checkbox-label">
                <input type="checkbox" role="switch" checked={pendingAutoStart ?? item.auto_start}
                    aria-label={`自动启动 ${item.module_id} 的接口`}
                    title={dirty ? '先保存接口选择' : '模块启动时恢复已保存的接口'}
                    disabled={!!busy || dirty || historical || item.superseded_by !== null}
                    onChange={(event) => void act('auto-start', undefined, event.target.checked)} />
                自动启动
            </label>
            <span>{item.saved_interface_ids.length ? `已保存 ${item.saved_interface_ids.length} 个接口` : '尚未保存接口'}</span>
            {item.state === 'pending' && item.auto_start && item.saved_interface_ids.length > 0 && item.error &&
                <Button variant="outline" size="sm" disabled={updating || !item.module_active}
                    onClick={() => void act('reapply')}>
                    {busy === 'reapply' ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}重试恢复
                </Button>}
        </div>
        {item.error && <Notice error>{item.error}</Notice>}
        {error && <Notice error>{error}</Notice>}
        {item.grants.length > 0 && <div className="network-request-grants" aria-label={`${item.module_id} 已分配接口`}>
            {item.grants.map((grant) => <div className="network-request-grant" key={grant.interface_id}
                data-grant-interface-id={grant.interface_id}>
                <div><strong>{catalog.interfaces.find((entry) => entry.interface_id === grant.interface_id)?.name
                    ?? '网络接口'}</strong><code>{grant.interface_id}</code></div>
                <Button variant="ghost" size="icon" disabled={updating || !editable} title="撤销此接口"
                    aria-label={`撤销 ${item.module_id} 的接口 ${grant.interface_id}`}
                    onClick={() => void act('unassign', grant.interface_id)}>
                    {busy === grant.interface_id ? <Loader2 size={14} className="spin" /> : <Unlink size={14} />}</Button>
            </div>)}
        </div>}
        {editable ? <div className="network-request-assignment">
            {available.length ? <fieldset className="network-assignment-options" disabled={updating}>
                <legend>分配接口 <span>{selected.length} / {item.requirement.max_interfaces}</span></legend>
                {available.map((entry) => {
                    const checked = selected.includes(entry.interface_id);
                    return <label key={entry.interface_id} className={`network-assignment-option ${checked ? 'selected' : ''}`}>
                        <input type="checkbox" checked={checked}
                            aria-label={`为 ${item.module_id} 分配 ${entry.name} (${entry.interface_id})`}
                            disabled={!checked && selected.length >= item.requirement.max_interfaces}
                            onChange={(event) => toggle(entry.interface_id, event.target.checked)} />
                        <span><strong>{entry.name}</strong><code>{entry.interface_id}</code></span>
                        <small>{catalog.types.find((definition) => definition.type === entry.type)?.label ?? entry.type}</small>
                    </label>;
                })}
            </fieldset> : <span className="muted">{catalog.interfaces.length ? '没有匹配的接口，请先新建接口。' : '先新建接口'}</span>}
            {invalid && <Notice error>所选接口已不可用，请重置选择后重新分配。</Notice>}
            <div className="network-assignment-footer">
                <span>{dirty ? '选择尚未保存' : `已分配 ${assigned.length} 个接口`}</span>
                <div>{dirty && <Button variant="ghost" size="sm" disabled={updating} onClick={() => setDraft(null)}>
                    <RotateCcw size={14} />重置选择</Button>}
                    {item.grants.length > 0 && <Button variant="outline" size="sm" disabled={updating}
                        onClick={() => void act('unassign')}>
                        {busy === 'unassign' ? <Loader2 size={14} className="spin" /> : <Unlink size={14} />}撤销全部
                    </Button>}
                    <Button size="sm" disabled={updating || !dirty || invalid} onClick={() => void act('assign')}>
                        {busy === 'assign' ? <Loader2 size={14} className="spin" /> : <Check size={14} />}保存分配
                    </Button></div>
            </div>
        </div> : <div className="network-request-reapply">
            <p className="section-note">{!item.module_active ? '模块尚未运行。'
                : historical ? '历史申请。' : '此申请已释放。'}</p>
            {item.state === 'released' && <Button variant="outline" size="sm"
                disabled={updating || !item.can_reapply || !item.module_active}
                title={!item.module_active ? '先启动模块' : '重新声明接口需求'}
                onClick={() => void act('reapply')}>
                {busy === 'reapply' ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}重新申请
            </Button>}
        </div>}
    </div>;
}

function RequestHistory({ current, catalog, refresh, reapplied }: {
    current: NetworkRequest[]; catalog: NetworkInterfaceCatalog;
    refresh: () => Promise<void>; reapplied: (requestId: string) => void;
}) {
    const history = useResource('/api/heredity/network-requests?include_history=true', parseNetworkRequests);
    const records = history.value ? historyNetworkRequests(history.value, current) : [];
    const version = current.map((item) => `${item.request_id}:${item.state}`).join(',');
    const seenVersion = useRef(version);
    useEffect(() => {
        if (seenVersion.current !== version) {
            seenVersion.current = version;
            void history.refresh();
        }
    }, [version, history.refresh]);
    const updated = useCallback(async () => {
        await Promise.all([refresh(), history.refresh()]);
    }, [refresh, history.refresh]);
    return <div className="network-request-history-body">
        {history.error && <Notice error>{history.error}</Notice>}
        {!history.value && history.loading ? <Loading text="正在读取申请历史" />
            : records.length === 0 && <Empty text="暂无历史申请" />}
        {records.map((item) => <details key={item.request_id} className="network-request-history-item">
            <summary><code>{item.module_id}</code><span>{item.requirement.account ?? '未绑定账号'}</span>
                <Badge>{REQUEST_STATE_LABELS[item.state]}</Badge>
                <time>{new Date(item.created_at * 1000).toLocaleString()}</time></summary>
            <ModuleRequest item={item} catalog={catalog} historical refresh={updated} reapplied={reapplied} />
        </details>)}
    </div>;
}

export function NetworkInterfacesPage() {
    const resource = useResource('/api/heredity/network-interfaces', parseNetworkInterfaces);
    const requests = useResource('/api/heredity/network-requests', parseNetworkRequests);
    const [editing, setEditing] = useState<NetworkInterface | null | undefined>(undefined);
    const [notice, setNotice] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [historyOpen, setHistoryOpen] = useState(false);
    const [focusRequest, setFocusRequest] = useState<string | null>(null);
    const refresh = useCallback(async () => {
        await Promise.all([resource.refresh(), requests.refresh()]);
    }, [resource.refresh, requests.refresh]);
    useEffect(() => {
        const timer = setInterval(() => void refresh(), 3000);
        return () => clearInterval(timer);
    }, [refresh]);
    useEffect(() => {
        if (!focusRequest || !requests.value?.some((item) => item.request_id === focusRequest)) return;
        document.querySelector(`[data-network-request-id="${CSS.escape(focusRequest)}"]`)
            ?.scrollIntoView({ block: 'center' });
        setFocusRequest(null);
    }, [focusRequest, requests.value]);
    const copy = async (value: string, label: string) => {
        try { await navigator.clipboard.writeText(value); setNotice(`${label}已复制。`); setError(null); }
        catch (failure) { setError(message(failure)); }
    };
    const catalog = resource.value;
    const currentRequests = currentNetworkRequests(requests.value ?? []);
    return <>
        <PageHeading title="网络接口" actions={<>
            <Button variant="outline" size="sm" disabled={resource.loading} onClick={() => void refresh()}>
                <RefreshCw size={14} className={resource.loading ? 'spin' : ''} />刷新
            </Button>
            <Button size="sm" disabled={!catalog || !catalog.types.some((entry) => entry.implemented)}
                onClick={() => setEditing(null)}><Plus size={15} />新建接口</Button>
        </>} />
        {resource.error && <Notice error>网络接口管理不可用：{resource.error}</Notice>}
        {error && <Notice error>{error}</Notice>}
        {notice && <Notice>{notice}</Notice>}
        {catalog && <div className="network-interface-summary">
            <Status value={catalog.module_bus.available} good="本体接口总线运行中" bad="接口总线不可用" />
            <span>{catalog.interfaces.length} 个接口</span>
            <span>{catalog.interfaces.filter((entry) => entry.state === 'running').length} 个运行中</span>
            <span><Network size={12} />16 位唯一 ID</span>
        </div>}
        {!catalog && resource.loading ? <Loading text="正在读取网络接口" />
            : catalog?.interfaces.length === 0 && <Empty text="尚未创建网络接口" />}
        {catalog?.interfaces.map((item) => <InterfaceRow key={item.interface_id} item={item} catalog={catalog}
            edit={() => setEditing(item)} refresh={refresh} copied={copy} />)}
        {catalog && <section className="section network-bus-info">
            <div className="section-heading"><h2><Activity size={16} />模块接口申请</h2>
                <Badge>{currentRequests.filter((item) => item.state === 'pending').length} 待分配</Badge></div>
            {requests.error && <Notice error>{requests.error}</Notice>}
            {!requests.value && requests.loading ? <Loading text="正在读取模块申请" />
                : currentRequests.length === 0 && <Empty text="暂无模块接口申请" />}
            <div className="network-current-requests">
                {currentRequests.map((item) => <ModuleRequest key={item.request_id} item={item}
                    catalog={catalog} refresh={refresh} reapplied={setFocusRequest} />)}
            </div>
            <details className="network-request-history" onToggle={(event) => setHistoryOpen(event.currentTarget.open)}>
                <summary>申请历史</summary>
                {historyOpen && <RequestHistory current={currentRequests} catalog={catalog}
                    refresh={refresh} reapplied={setFocusRequest} />}
            </details>
        </section>}
        {editing !== undefined && catalog && <InterfaceEditor source={editing} catalog={catalog}
            close={() => setEditing(undefined)} saved={refresh} />}
    </>;
}
