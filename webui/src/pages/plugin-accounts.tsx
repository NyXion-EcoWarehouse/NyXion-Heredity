import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, Loader2, Plus, RefreshCw, RotateCcw, X } from 'lucide-react';
import { api, message, post } from '../api';
import { useResource } from '../hooks';
import { parseAccountSelection, parsePluginCatalog, pluginInstancePath,
    type PluginNavigation } from '../plugin-model';
import { Button, Empty, Loading, Notice, PageHeading, Status } from '../ui';
import { PluginPanel } from './plugins';

export function PluginAccountsPage({ page, navigate }: {
    page: PluginNavigation; navigate: (path: string) => void;
}) {
    const resource = useResource('/api/heredity/plugins', parsePluginCatalog);
    const selection = useResource('/api/heredity/account-selection', parseAccountSelection);
    const [adding, setAdding] = useState(false);
    const [name, setName] = useState('');
    const [busy, setBusy] = useState(false);
    const [selectionBusy, setSelectionBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const mutation = useRef(false);
    const refresh = useCallback(async () => {
        await Promise.all([resource.refresh(), selection.refresh()]);
    }, [resource.refresh, selection.refresh]);
    useEffect(() => {
        const timer = setInterval(() => { void refresh(); }, 3000);
        return () => clearInterval(timer);
    }, [refresh]);
    const plugin = resource.value?.plugins.find((item) => item.id === page.plugin_id);
    const add = async (event: React.FormEvent) => {
        event.preventDefault();
        if (mutation.current || !name.trim()) return;
        mutation.current = true; setBusy(true); setError(null);
        try {
            const key = 'account-' + crypto.randomUUID().replaceAll('-', '');
            await post(`/api/heredity/plugins/${encodeURIComponent(page.plugin_id)}/instances`,
                { instance_key: key, display_name: name.trim() });
            setName(''); setAdding(false);
            await refresh();
        } catch (failure) { setError(message(failure)); }
        finally { mutation.current = false; setBusy(false); }
    };
    const useAutomaticSelection = async () => {
        if (selectionBusy) return;
        setSelectionBusy(true); setError(null);
        try {
            await post('/api/heredity/account-selection', { account: null });
            await selection.refresh();
        } catch (failure) { setError(message(failure)); }
        finally { setSelectionBusy(false); }
    };
    const selectedAccount = selection.value?.account;
    return <>
        <PageHeading title={page.title} actions={<>
            <Button variant="ghost" size="sm" onClick={() => navigate('/plugins')}>
                <ArrowLeft size={14} />插件
            </Button>
            <Button variant="outline" size="sm" disabled={resource.loading}
                onClick={() => void refresh()}>
                <RefreshCw size={14} className={resource.loading ? 'spin' : ''} />刷新
            </Button>
            <Button size="sm" disabled={!resource.value?.host_running || !plugin}
                onClick={() => setAdding(true)}><Plus size={15} />添加账号</Button>
        </>} />
        {[resource.error, selection.error, error].filter(Boolean).map((value, index) =>
            <Notice error key={`${index}:${value}`}>{value}</Notice>)}
        {plugin && <div className="plugin-host-line">
            <Status value={resource.value?.host_running} good="插件宿主运行中" bad="插件宿主未运行" />
            <span>{plugin.instances.length} 个账号位</span>
            <span>{plugin.instances.filter((item) => item.session?.state === 'online').length} 个在线账号</span>
            <span>OneBot 当前账号：<code>{selectedAccount ?? '自动选择唯一在线账号'}</code></span>
        </div>}
        {selection.value?.state === 'account_selection_required' &&
            <Notice>有多个在线账号，请为 OneBot 选择当前账号。</Notice>}
        {selectedAccount && <div className="plugin-selection-actions">
            <Button variant="ghost" size="sm" disabled={selectionBusy}
                onClick={() => void useAutomaticSelection()}>
                {selectionBusy ? <Loader2 size={14} className="spin" /> : <RotateCcw size={14} />}
                恢复自动选择
            </Button>
        </div>}
        {adding && <section className="section plugin-add-section">
            <form onSubmit={(event) => void add(event)} className="plugin-add-form">
                <label className="field">账号名称<input autoFocus value={name} maxLength={80}
                    placeholder="例如：测试账号 A" disabled={busy}
                    onChange={(event) => setName(event.target.value)} /></label>
                <div className="plugin-add-actions">
                    <Button type="submit" disabled={busy || !name.trim()}>
                        {busy ? <Loader2 size={14} className="spin" /> : <Plus size={14} />}添加
                    </Button>
                    <Button type="button" variant="ghost" size="icon" title="取消添加" aria-label="取消添加"
                        disabled={busy} onClick={() => { setAdding(false); setError(null); }}><X size={17} /></Button>
                </div>
            </form>
        </section>}
        {!resource.value && resource.loading ? <Loading text="正在读取账号" />
            : !plugin ? <Empty text="此插件页面已不可用" />
                : plugin.instances.length === 0 ? <Empty text="尚未添加账号" />
                    : plugin.instances.map((instance) => {
                        const account = instance.session?.self_id
                            ? `${instance.session.platform}:${instance.session.self_id}` : null;
                        const selected = account !== null && (selectedAccount === account
                            || (selectedAccount === null && selection.value?.accounts.length === 1));
                        return <PluginPanel key={`${plugin.id}:${instance.instance_key}`} plugin={instance}
                            instance hostRunning={resource.value!.host_running} refresh={refresh}
                            selected={selected}
                            selectAccount={account ? async () => {
                                await post('/api/heredity/account-selection', { account });
                            } : undefined}
                            remove={async () => {
                                await api(pluginInstancePath(plugin.id, instance.instance_key), { method: 'DELETE' });
                            }} />;
                    })}
    </>;
}
