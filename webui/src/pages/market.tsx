import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import {
    AlertTriangle, Ban, CheckCircle2, Clock3, Download, Loader2, Package,
    Play, RefreshCw, Search, Shield, ShieldAlert, Square, X,
} from 'lucide-react';
import { api, message, post } from '../api';
import { useResource } from '../hooks';
import {
    isMarketJobActive, marketInstallPayload, marketJobPath, marketModulePath, MARKET_JOB_LABELS,
    packageCompatible, packageSourceLabel, parseMarketCatalog, parseMarketJob, parseMarketModules,
    permissionDescription, type MarketJob, type MarketModule, type MarketPackage, type MarketSandbox,
} from '../market-model';
import { Badge, Button, Empty, Loading, Notice, PageHeading, Status } from '../ui';

function date(value: number | null) {
    return value === null ? '尚未成功刷新' : new Date(value * 1000).toLocaleString();
}

function SandboxStatus({ value }: { value: MarketSandbox }) {
    return <div className="market-runtime-status">
        <Status value={value.available} good="商场 WASM 模块隔离可用"
            bad="商场 WASM 模块隔离未就绪" unknown="商场 WASM 模块隔离状态未知" />
        {value.runtime && <span>运行时 <code>{value.runtime}</code></span>}
        {value.isolation && <span>隔离方式 <code>{value.isolation}</code></span>}
        {value.reason && <p>{value.reason}</p>}
    </div>;
}

function InstallDialog({ item, current, hostVersion, sandbox, stale, close, installed }: {
    item: MarketPackage; current: MarketPackage | undefined; hostVersion: string;
    sandbox: MarketSandbox; stale: boolean; close: () => void; installed: (job: MarketJob) => Promise<void>;
}) {
    const [confirmed, setConfirmed] = useState<string[]>([]);
    const [forceWarning, setForceWarning] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const guard = useRef(false);
    const dialog = useRef<HTMLElement | null>(null);
    const changed = !current || current.hash_sha256 !== item.hash_sha256
        || current.version !== item.version || current.status !== item.status
        || JSON.stringify(current.permissions) !== JSON.stringify(item.permissions)
        || current.security_advisory !== item.security_advisory;
    const compatible = packageCompatible(item, hostVersion);
    const allowed = !changed && !stale && compatible && item.status !== 'banned'
        && confirmed.length === item.permissions.length && (item.status !== 'warning' || forceWarning);
    useEffect(() => {
        const previous = document.activeElement as HTMLElement | null;
        dialog.current?.querySelector<HTMLButtonElement>('button')?.focus();
        return () => previous?.focus();
    }, []);
    const keyboard = (event: KeyboardEvent<HTMLElement>) => {
        if (event.key === 'Escape' && !busy) { event.preventDefault(); close(); }
        if (event.key !== 'Tab') return;
        const controls = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)') ?? [])];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    const install = async () => {
        if (guard.current || !allowed) return;
        let payload;
        try { payload = marketInstallPayload(item, hostVersion, confirmed, forceWarning); }
        catch (failure) { setError(message(failure)); return; }
        guard.current = true; setBusy(true); setError(null);
        try {
            const job = parseMarketJob(await post('/api/heredity/market/install', payload));
            await installed(job);
            close();
        } catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(false); }
    };
    return <div className="modal-backdrop" onClick={(event) => {
        if (event.target === event.currentTarget && !busy) close();
    }}>
        <section className="dialog market-install-dialog" role="dialog" aria-modal="true"
            aria-labelledby="market-install-title" ref={dialog} onKeyDown={keyboard}>
            <div className="section-heading">
                <h2 id="market-install-title"><Package size={18} />安装 {item.name}</h2>
                <Button variant="ghost" size="icon" disabled={busy} title="关闭" aria-label="关闭安装确认"
                    onClick={close}><X size={18} /></Button>
            </div>
            {changed && <Notice error>目录中的模块已更新，请关闭后重新选择。</Notice>}
            {stale && <Notice error>请先成功刷新目录，再确认安装。</Notice>}
            {error && <Notice error>{error}</Notice>}
            <dl className="market-package-facts">
                <div><dt>模块</dt><dd><code>{item.id}</code></dd></div>
                <div><dt>版本</dt><dd>{item.version}</dd></div>
                <div><dt>作者</dt><dd>{item.author.name}</dd></div>
                <div><dt>最低宿主版本</dt><dd><code>{item.min_heredity_version}</code>
                    <Badge variant={compatible ? 'success' : 'destructive'}>
                        {compatible ? `当前 ${hostVersion}` : `当前 ${hostVersion} 不兼容`}</Badge></dd></div>
            </dl>
            <div className="market-package-source"><span>包来源</span><code>{packageSourceLabel(item.download_url)}</code></div>
            <div className="market-package-source"><span>SHA-256</span><code>{item.hash_sha256}</code></div>
            {item.status === 'warning' && <div className="market-advisory" role="alert">
                <div><AlertTriangle size={16} /><strong>安全提示</strong></div>
                <p>{item.security_advisory || '此模块被目录标记为警告状态。'}</p>
                <label className="checkbox-label"><input type="checkbox" checked={forceWarning}
                    disabled={busy || changed} onChange={(event) => setForceWarning(event.target.checked)} />
                    我已阅读安全提示，仍要强制安装</label>
            </div>}
            <div className="market-permissions">
                <h3><Shield size={15} />申请权限</h3>
                {item.permissions.length ? item.permissions.map((permission) => {
                    const detail = permissionDescription(permission);
                    return <label key={permission} className="market-permission">
                        <input type="checkbox" checked={confirmed.includes(permission)} disabled={busy || changed}
                            aria-label={`允许 ${detail.label} (${permission})`}
                            onChange={(event) => setConfirmed((values) => event.target.checked
                                ? [...values, permission] : values.filter((value) => value !== permission))} />
                        <span><strong>{detail.label}<code>{permission}</code></strong><small>{detail.description}</small></span>
                    </label>;
                }) : <p className="section-note">模块未申请额外权限。</p>}
            </div>
            <SandboxStatus value={sandbox} />
            <div className="market-dialog-footer"><Button variant="ghost" disabled={busy} onClick={close}>取消</Button>
                <Button variant={item.status === 'warning' ? 'danger' : 'primary'} disabled={busy || !allowed}
                    onClick={() => void install()}>
                    {busy ? <Loader2 size={15} className="spin" /> : <Download size={15} />}
                    {item.status === 'warning' ? '强制安装' : '确认安装'}
                </Button></div>
        </section>
    </div>;
}

function MarketItem({ item, hostVersion, install, installing, stale }: {
    item: MarketPackage; hostVersion: string; install: () => void; installing: boolean; stale: boolean;
}) {
    const compatible = packageCompatible(item, hostVersion);
    const banned = item.status === 'banned';
    const installedCurrent = item.installed && (item.installed_version === null || item.installed_version === item.version);
    return <article className={`market-item market-item-${item.status}`} data-market-plugin-id={item.id}>
        <div className="market-item-heading"><div className="market-item-name"><Package size={20} />
            <div><h2>{item.name}</h2><code>{item.id}</code></div></div>
            <div className="market-item-badges">
                {item.installed && <Badge variant="success">已安装{item.installed_version ? ` ${item.installed_version}` : ''}</Badge>}
                <Badge variant={banned ? 'destructive' : item.status === 'warning' ? 'warning' : 'secondary'}>
                    {banned ? '已封禁' : item.status === 'warning' ? '安全警告' : '可安装'}</Badge>
                <span>v{item.version}</span>
            </div>
        </div>
        <p className="market-item-description">{item.description}</p>
        {item.security_advisory && <div className={`market-item-advisory ${banned ? 'banned' : ''}`}>
            {banned ? <Ban size={14} /> : <ShieldAlert size={14} />}<span>{item.security_advisory}</span></div>}
        <div className="market-item-meta"><span>作者 {item.author.name}</span>
            <span>{item.permissions.length} 项权限</span>
            <span className={compatible ? '' : 'error-text'}>宿主 ≥ {item.min_heredity_version}</span>
        </div>
        <div className="market-item-footer"><code>{packageSourceLabel(item.download_url)}</code>
            {banned ? <span className="market-banned-label"><Ban size={14} />禁止安装</span>
                : <Button size="sm" variant={item.status === 'warning' ? 'danger' : 'primary'}
                    disabled={!compatible || installing || installedCurrent || stale} onClick={install}
                    title={stale ? '请先刷新目录' : !compatible ? '请先升级宿主'
                        : installedCurrent ? '此版本已经安装' : '查看权限并安装'}>
                    {installing ? <Loader2 size={14} className="spin" /> : <Download size={14} />}
                    {installing ? '安装中' : installedCurrent ? '已安装' : item.status === 'warning' ? '强制安装' : item.installed ? '更新' : '安装'}
                </Button>}
        </div>
    </article>;
}

function JobRow({ job }: { job: MarketJob }) {
    const active = isMarketJobActive(job);
    const installedNotStarted = job.result?.state === 'installed_not_started';
    const completed = job.state === 'completed';
    const label = { refresh: '刷新模块目录', install: `安装 ${job.plugin_id}`,
        module_start: `启动 ${job.plugin_id}`, module_stop: `停止 ${job.plugin_id}`,
        module_restart: `重启 ${job.plugin_id}` }[job.kind];
    const nestedError = job.result?.error;
    return <div className="market-job" data-market-job-id={job.job_id}>
        <div className="market-job-title">
            {active ? <Loader2 size={15} className="spin" /> : completed ? <CheckCircle2 size={15} />
                : <AlertTriangle size={15} />}
            <strong>{label}</strong>
            <Badge variant={job.state === 'failed' ? 'destructive' : completed ? 'success' : 'secondary'}>
                {installedNotStarted ? '已安装，未启动' : completed && job.kind === 'install' ? '已安装' : MARKET_JOB_LABELS[job.state]}
            </Badge>
            <time>{new Date(job.updated_at * 1000).toLocaleTimeString()}</time>
        </div>
        {job.error && <Notice error>{job.error.message}<code>{job.error.code}</code></Notice>}
        {installedNotStarted && <p>安装已完成，模块尚未启动。</p>}
        {nestedError !== null && typeof nestedError === 'object' && 'message' in nestedError
            && typeof nestedError.message === 'string' && <Notice error>{nestedError.message}</Notice>}
    </div>;
}

function InstalledModule({ item, working, submitted }: {
    item: MarketModule; working: boolean; submitted: (job: MarketJob) => Promise<void>;
}) {
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const guard = useRef(false);
    const run = async (command: 'start' | 'stop' | 'restart') => {
        if (guard.current) return;
        guard.current = true; setBusy(command); setError(null);
        try { await submitted(parseMarketJob(await post(marketModulePath(item.id, command), {}))); }
        catch (failure) { setError(message(failure)); }
        finally { guard.current = false; setBusy(null); }
    };
    const active = ['running', 'starting'].includes(item.state);
    const disabled = !!busy || working || item.state === 'starting';
    const states = { installed: '已安装', starting: '启动中', running: '运行中', stopped: '已停止', failed: '异常' };
    return <div className="market-module" data-market-module-id={item.id}>
        <div className="market-module-heading">
            <div><h3>{item.name}<span>v{item.version}</span></h3><code>{item.namespace}</code></div>
            <div><Badge variant={item.state === 'running' ? 'success' : item.state === 'failed' ? 'destructive' : 'secondary'}>
                {states[item.state]}</Badge>
                <Button variant="outline" size="sm" disabled={disabled} aria-label={`重启模块 ${item.name}`}
                    onClick={() => void run('restart')}><RefreshCw size={13} className={busy === 'restart' ? 'spin' : ''} />重启</Button>
                <Button variant={active ? 'outline' : 'primary'} size="sm" disabled={disabled}
                    aria-label={`${active ? '停止' : '启动'}模块 ${item.name}`}
                    onClick={() => void run(active ? 'stop' : 'start')}>
                    {busy ? <Loader2 size={13} className="spin" /> : active ? <Square size={13} /> : <Play size={13} />}
                    {active ? '停止' : '启动'}</Button>
            </div>
        </div>
        <div className="market-module-status"><Status value={item.sandbox.process_running ? item.sandbox.isolated : null}
            good="独立进程与 WASM 隔离运行中" bad="运行隔离未启用" unknown="模块未运行" />
            <span>{item.permissions.length} 项已确认权限</span>
            <code>{item.sandbox.runtime} · {item.sandbox.isolation}</code></div>
        {[error, item.error?.message].filter(Boolean).map((value, index) =>
            <Notice error key={`${index}:${value}`}>{value}</Notice>)}
    </div>;
}

export function MarketPage({ hostVersion }: { hostVersion: string }) {
    const catalog = useResource('/api/heredity/market', parseMarketCatalog);
    const modules = useResource('/api/heredity/market/modules', parseMarketModules);
    const [selection, setSelection] = useState<MarketPackage | null>(null);
    const [search, setSearch] = useState('');
    const [status, setStatus] = useState('all');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [jobs, setJobs] = useState<MarketJob[]>([]);
    const refreshGuard = useRef(false);
    const remember = useCallback((incoming: MarketJob[]) => {
        setJobs((current) => {
            const merged = new Map(current.map((job) => [job.job_id, job]));
            for (const job of incoming) {
                const previous = merged.get(job.job_id);
                if (!previous || job.updated_at >= previous.updated_at) merged.set(job.job_id, job);
            }
            return [...merged.values()].sort((first, second) => second.created_at - first.created_at).slice(0, 32);
        });
    }, []);
    useEffect(() => { if (catalog.value) remember(catalog.value.jobs); }, [catalog.value, remember]);
    useEffect(() => { if (modules.value) remember(modules.value.jobs); }, [modules.value, remember]);
    useEffect(() => {
        if (!modules.value?.modules.length) return;
        const timer = setInterval(() => void modules.refresh(), 3000);
        return () => clearInterval(timer);
    }, [modules.value?.modules.length, modules.refresh]);
    const activeIds = jobs.filter(isMarketJobActive).map((job) => job.job_id).join(',');
    useEffect(() => {
        if (!activeIds) return;
        let alive = true;
        let controller: AbortController | null = null;
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            controller = new AbortController();
            try {
                const updated = await Promise.all(activeIds.split(',').map(async (id) =>
                    parseMarketJob(await api(marketJobPath(id), { signal: controller!.signal }))));
                if (!alive) return;
                remember(updated); setError(null);
                if (updated.some((job) => !isMarketJobActive(job))) {
                    await Promise.all([catalog.refresh(), modules.refresh()]);
                }
            } catch (failure) {
                if (alive && !controller.signal.aborted) setError(message(failure));
            } finally { if (alive) timer = setTimeout(() => void poll(), 1200); }
        };
        void poll();
        return () => { alive = false; clearTimeout(timer); controller?.abort(); };
    }, [activeIds, remember, catalog.refresh, modules.refresh]);
    const refresh = async () => {
        if (refreshGuard.current) return;
        refreshGuard.current = true; setBusy(true); setError(null);
        try {
            remember([parseMarketJob(await post('/api/heredity/market/refresh', {}))]);
            await Promise.all([catalog.refresh(), modules.refresh()]);
        } catch (failure) { setError(message(failure)); }
        finally { refreshGuard.current = false; setBusy(false); }
    };
    const source = catalog.value;
    const packages = source?.plugins.filter((item) =>
        (status === 'all' || item.status === status)
        && [item.name, item.id, item.description, item.author.name].join(' ').toLocaleLowerCase()
            .includes(search.trim().toLocaleLowerCase())) ?? [];
    return <>
        <PageHeading title="模块商场" actions={<Button variant="outline" size="sm"
            disabled={busy || source?.refreshing || jobs.some((job) => job.kind === 'refresh' && isMarketJobActive(job))}
            onClick={() => void refresh()}>
            <RefreshCw size={14} className={busy || source?.refreshing ? 'spin' : ''} />刷新目录
        </Button>} />
        {catalog.error && <Notice error>模块商场不可用：{catalog.error}</Notice>}
        {error && <Notice error>{error}</Notice>}
        {source && <>
            <section className="market-source">
                <div><span>目录来源</span><code>{packageSourceLabel(source.index_url)}</code></div>
                <div><Clock3 size={13} /><span>最近成功刷新 {date(source.last_success_at)}</span>
                    {source.stale && <Badge variant="warning">缓存目录</Badge>}</div>
                <SandboxStatus value={source.sandbox} />
            </section>
            {source.error && <Notice error>{source.error.message}<code>{source.error.code}</code></Notice>}
            <div className="market-toolbar">
                <label className="search-field"><Search size={14} /><input aria-label="搜索模块"
                    placeholder="搜索名称、作者或模块 ID" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
                <select aria-label="模块安全状态" value={status} onChange={(event) => setStatus(event.target.value)}>
                    <option value="all">全部状态</option><option value="active">可安装</option>
                    <option value="warning">安全警告</option><option value="banned">已封禁</option>
                </select>
                <span>{source.plugins.length} 个模块</span>
            </div>
            {packages.length ? <div className="market-grid">
                {packages.map((item) => <MarketItem key={item.id} item={item} hostVersion={hostVersion} stale={source.stale}
                    install={() => setSelection(item)} installing={jobs.some((job) =>
                        job.plugin_id === item.id && job.kind === 'install' && isMarketJobActive(job))} />)}
            </div> : <Empty text={search || status !== 'all' ? '没有匹配的模块' : '目录中暂无可安装模块'} />}
            {source.invalid_entries.length > 0 && <section className="section market-invalid">
                <div className="section-heading"><h2><ShieldAlert size={16} />不可用目录条目</h2>
                    <Badge variant="warning">{source.invalid_entries.length} 项</Badge></div>
                {source.invalid_entries.map((entry) => <div key={`${entry.position}:${entry.name}`}>
                    <strong>{entry.name || `条目 ${entry.position}`}</strong><Badge variant="destructive">无法安装</Badge>
                    <p>{entry.error.message}</p><code>{entry.error.code}</code>
                </div>)}
            </section>}
        </>}
        {!source && catalog.loading && <Loading text="正在读取模块目录" />}
        <section className="section market-modules">
            <div className="section-heading"><h2>已安装模块</h2><Badge>{modules.value?.modules.length ?? 0}</Badge></div>
            {modules.error && <Notice error>{modules.error}</Notice>}
            {!modules.value && modules.loading ? <Loading text="正在读取已安装模块" />
                : modules.value?.modules.length === 0 && <Empty text="尚未安装商场模块" />}
            {modules.value?.modules.map((item) => <InstalledModule key={item.id} item={item}
                working={jobs.some((job) => job.plugin_id === item.id && isMarketJobActive(job))}
                submitted={async (job) => {
                    remember([job]); await Promise.all([catalog.refresh(), modules.refresh()]);
                }} />)}
        </section>
        {jobs.length > 0 && <section className="section market-jobs">
            <div className="section-heading"><h2>任务记录</h2><Badge>{jobs.length}</Badge></div>
            {jobs.map((job) => <JobRow key={job.job_id} job={job} />)}
        </section>}
        {selection && source && <InstallDialog key={`${selection.id}:${selection.hash_sha256}`} item={selection}
            current={source.plugins.find((item) => item.id === selection.id)}
            hostVersion={hostVersion} sandbox={source.sandbox} stale={source.stale} close={() => setSelection(null)}
            installed={async (job) => {
                remember([job]); await Promise.all([catalog.refresh(), modules.refresh()]);
            }} />}
    </>;
}
