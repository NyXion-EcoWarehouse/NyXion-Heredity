import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Loader2, RefreshCw, Save, Settings2 } from 'lucide-react';
import { api, message } from '../api';
import { useResource } from '../hooks';
import { INTERFACE_STATE_LABELS } from '../network-interfaces-model';
import { hostSettingsPayload, parseHostSettings } from '../settings-model';
import { Badge, Button, Empty, Loading, Notice, PageHeading } from '../ui';

export function SettingsPage({ navigate }: { navigate: (path: string) => void }) {
    const resource = useResource('/api/heredity/settings', parseHostSettings);
    const [draft, setDraft] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);
    const guard = useRef(false);
    const saved = resource.value;
    const port = draft ?? String(saved?.startup.configured_ui_port ?? '');
    const dirty = saved && port !== String(saved.startup.configured_ui_port);
    useEffect(() => {
        const timer = setInterval(() => void resource.refresh(), 3000);
        return () => clearInterval(timer);
    }, [resource.refresh]);
    const save = async (event: React.FormEvent) => {
        event.preventDefault();
        if (guard.current) return;
        let payload;
        try { payload = hostSettingsPayload(port); }
        catch (failure) { setError(message(failure)); return; }
        guard.current = true; setBusy(true); setError(null); setNotice(null);
        try {
            const value = parseHostSettings(await api('/api/heredity/settings', {
                method: 'PUT', body: JSON.stringify(payload),
            }));
            resource.setValue(value); setDraft(null);
            setNotice(`WebUI 端口已保存到 ${value.startup.env_file}，下次宿主重启后生效。`);
        } catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(false); }
    };
    return <>
        <PageHeading title="配置" actions={<Button variant="outline" size="sm" disabled={resource.loading}
            onClick={() => void resource.refresh()}><RefreshCw size={14} className={resource.loading ? 'spin' : ''} />刷新</Button>} />
        {resource.error && <Notice error>{resource.error}</Notice>}
        {error && <Notice error>{error}</Notice>}
        {notice && <Notice>{notice}</Notice>}
        {!saved && resource.loading && <Loading text="正在读取宿主配置" />}
        {saved && <>
            <section className="section settings-startup">
                <div className="section-heading"><h2><Settings2 size={16} />管理服务</h2>
                    {saved.startup.restart_required && <Badge variant="warning">重启后生效</Badge>}</div>
                <dl className="fact-grid settings-facts">
                    <div><dt>监听地址</dt><dd><code>{saved.startup.ui_host}</code></dd></div>
                    <div><dt>当前 WebUI 端口</dt><dd><code>{saved.startup.ui_port}</code></dd></div>
                    <div><dt>下次启动端口</dt><dd><code>{saved.startup.configured_ui_port}</code></dd></div>
                    <div><dt>配置文件</dt><dd><code>{saved.startup.env_file}</code></dd></div>
                </dl>
                <form className="settings-port-form" onSubmit={(event) => void save(event)}>
                    <label className="field">WebUI 端口<input type="number" inputMode="numeric"
                        min={1} max={65535} step={1} required value={port} disabled={busy}
                        onChange={(event) => { setDraft(event.target.value); setError(null); setNotice(null); }} />
                        <small>保存后，下次宿主重启时使用此端口。</small></label>
                    <Button type="submit" disabled={busy || !dirty}>
                        {busy ? <Loader2 size={14} className="spin" /> : <Save size={14} />}保存端口</Button>
                </form>
            </section>
            <section className="section settings-services">
                <div className="section-heading"><h2>网络服务端口</h2>
                    <Button variant="outline" size="sm" onClick={() => navigate('/connections')}>
                        网络接口<ArrowRight size={14} /></Button></div>
                {saved.services.length ? <div className="table-wrap settings-services-table">
                    <table><thead><tr><th>服务 / 接口 ID</th><th>状态</th><th>配置端口</th><th>实际端口</th><th>端点</th></tr></thead>
                        <tbody>{saved.services.map((item) => <tr key={item.interface_id}>
                            <td><strong>{item.name}</strong><code>{item.interface_id}</code></td>
                            <td><Badge variant={item.state === 'error' ? 'destructive'
                                : item.state === 'running' ? 'success' : 'secondary'}>{INTERFACE_STATE_LABELS[item.state]}</Badge>
                                {item.error && <small className="error-text">{item.error}</small>}</td>
                            <td>{item.type.endsWith('_client') ? '远端' : item.port === 0 ? '自动分配' : item.port}</td>
                            <td>{item.actual_port ?? '未监听'}</td>
                            <td><code>{item.endpoint ?? '尚未启动'}</code></td>
                        </tr>)}</tbody></table>
                </div> : <Empty text="尚未注册网络服务" />}
                <p className="section-note">每个网络接口独立配置端口。请在“网络接口”中编辑对应服务。</p>
            </section>
        </>}
    </>;
}
