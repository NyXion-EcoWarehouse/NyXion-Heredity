import { Component, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import {
    Activity, AlertTriangle, ArrowRight, Boxes, Bug, ChevronRight, Eye, EyeOff, LayoutDashboard,
    Link2, Loader2, LogOut, Menu, Plug, Radio, Settings, Store, Terminal, Users, X, type LucideIcon,
} from 'lucide-react';
import { api, asObject, getToken, message, post, saveToken } from './api';
import { Button, Loading, Notice } from './ui';
import { OverviewPage } from './pages/overview';
import { ExternalConnectionsPage } from './pages/external-connections';
import { DebugPage } from './pages/debug';
import { LogsPage } from './pages/logs';
import { PluginsPage } from './pages/plugins';
import { PluginAccountsPage } from './pages/plugin-accounts';
import { NetworkInterfacesPage } from './pages/network-interfaces';
import { MarketPage } from './pages/market';
import { SettingsPage } from './pages/settings';
import { attentionForPath, parseHostAttention, type AttentionItem } from './attention-model';
import { hostRelease } from './models';
import { mergePluginNavigation, parsePluginCatalog, type PluginNavigation } from './plugin-model';
import { ExecutionPanel } from '../execution-panel';

interface NavigationItem {
    path: string; label: string; icon: LucideIcon;
}
const NAVIGATION: NavigationItem[] = [
    { path: '/', label: '运行概览', icon: LayoutDashboard },
    { path: '/plugins', label: '插件', icon: Plug },
    { path: '/market', label: '模块商场', icon: Store },
    { path: '/connections', label: '网络接口', icon: Link2 },
    { path: '/external-connections', label: '外部 OneBot', icon: Radio },
    { path: '/execution', label: '执行级别', icon: Boxes },
    { path: '/debug', label: 'OneBot 调试', icon: Bug },
    { path: '/settings', label: '配置', icon: Settings },
    { path: '/logs', label: '运行日志', icon: Terminal },
];

function Logo({ compact = false }: { compact?: boolean }) {
    return <div className={`brand ${compact ? 'brand-compact' : ''}`}>
        <img className="brand-symbol" src="/heredity.svg" alt="" width={39} height={39} />
        <div><strong>NyXion Heredity</strong><span>OneBot 运行时</span></div>
    </div>;
}

function Login({ onLogin }: { onLogin: () => Promise<void> }) {
    const [password, setPassword] = useState('');
    const [visible, setVisible] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const submit = async (event: React.FormEvent) => {
        event.preventDefault();
        if (busy || !password) return;
        setBusy(true); setError(null);
        try {
            const result = asObject(await post('/api/login', { password }), '登录响应');
            if (typeof result.token !== 'string' || !result.token) throw new Error('登录响应缺少会话令牌');
            saveToken(result.token);
            setPassword('');
            await onLogin();
        } catch (failure) {
            setError(message(failure));
        } finally {
            setBusy(false);
        }
    };
    return <main className="login-page">
        <div className="login-console">
            <Logo />
            <div className="login-heading"><h1>登录控制台</h1><p>连接此设备上的 NyXion Heredity 管理服务。</p></div>
            {error && <Notice error>{error}</Notice>}
            <form onSubmit={(event) => void submit(event)}>
                <label className="field">访问密码<div className="password-field">
                    <input type={visible ? 'text' : 'password'} autoComplete="current-password" autoFocus
                        value={password} disabled={busy} placeholder="输入管理密码"
                        onChange={(event) => setPassword(event.target.value)} />
                    <Button type="button" variant="ghost" size="icon"
                        aria-label={visible ? '隐藏密码' : '显示密码'} title={visible ? '隐藏密码' : '显示密码'}
                        onClick={() => setVisible(!visible)}>
                        {visible ? <EyeOff size={16} /> : <Eye size={16} />}</Button>
                </div></label>
                <Button type="submit" disabled={busy || !password} className="login-submit">
                    {busy ? <Loader2 size={16} className="spin" /> : <ArrowRight size={16} />}
                    {busy ? '正在登录' : '登录'}
                </Button>
            </form>
            <div className="login-foot"><span className="local-dot" />本机管理端<span>NyXion Heredity</span></div>
        </div>
    </main>;
}

export default function App() {
    const [authenticated, setAuthenticated] = useState(false);
    const [checking, setChecking] = useState(true);
    const [page, setPage] = useState(window.location.pathname);
    const [mobileOpen, setMobileOpen] = useState(false);
    const [release, setRelease] = useState(() => hostRelease({}));
    const [loggingOut, setLoggingOut] = useState(false);
    const [pluginHost, setPluginHost] = useState(false);
    const [pluginPages, setPluginPages] = useState<PluginNavigation[]>([]);
    const [pluginPagesLoading, setPluginPagesLoading] = useState(false);
    const [pluginPagesError, setPluginPagesError] = useState<string | null>(null);
    const [attention, setAttention] = useState<AttentionItem[]>([]);
    const scrollArea = useRef<HTMLElement | null>(null);
    const pageContent = useRef<HTMLDivElement | null>(null);
    const scrollPositions = useRef(new Map<string, number>());
    const restoringScroll = useRef<number | null>(null);
    const currentPage = useRef(page);

    useEffect(() => {
        const previousRestoration = window.history.scrollRestoration;
        window.history.scrollRestoration = 'manual';
        const expired = () => { saveToken(null); setAuthenticated(false); };
        const route = () => {
            scrollPositions.current.set(currentPage.current, scrollArea.current?.scrollTop ?? 0);
            setPage(window.location.pathname); setMobileOpen(false);
        };
        window.addEventListener('heredity:session-expired', expired);
        window.addEventListener('popstate', route);
        let current = true;
        if (getToken()) {
            api('/api/status').then((value) => {
                const status = asObject(value);
                if (current) {
                    setAuthenticated(true);
                    setRelease((previous) => hostRelease(status, previous));
                    setPluginHost(status.plugin_host === true);
                }
            }).catch(() => { if (current) setAuthenticated(false); })
                .finally(() => { if (current) setChecking(false); });
        } else setChecking(false);
        return () => {
            current = false;
            window.removeEventListener('heredity:session-expired', expired);
            window.removeEventListener('popstate', route);
            window.history.scrollRestoration = previousRestoration;
        };
    }, []);

    useLayoutEffect(() => {
        currentPage.current = page;
        const area = scrollArea.current, content = pageContent.current;
        if (!authenticated || !area || !content) return;
        const target = scrollPositions.current.get(page) ?? 0;
        restoringScroll.current = target;
        const restore = () => {
            if (restoringScroll.current === null) return;
            area.scrollTop = restoringScroll.current;
            if (Math.abs(area.scrollTop - restoringScroll.current) < 1) restoringScroll.current = null;
        };
        restore();
        const observer = new ResizeObserver(restore);
        observer.observe(content);
        return () => {
            restoringScroll.current = null;
            observer.disconnect();
        };
    }, [authenticated, page]);

    useEffect(() => {
        if (!authenticated) return;
        let current = true;
        api('/api/status').then((value) => {
            if (!current) return;
            const status = asObject(value);
            setPluginHost(status.plugin_host === true);
            setRelease((previous) => hostRelease(status, previous));
        }).catch(() => { /* The authenticated resource views report availability. */ });
        return () => { current = false; };
    }, [authenticated]);

    useEffect(() => {
        if (!authenticated) { setAttention([]); return; }
        let alive = true;
        let timer: ReturnType<typeof setTimeout>;
        let controller: AbortController | null = null;
        const load = async () => {
            controller = new AbortController();
            try {
                const value = parseHostAttention(await api('/api/heredity/attention', { signal: controller.signal }));
                if (alive) setAttention(value.items);
            } catch {
                // Only host-reported findings become operator warnings.
            } finally { if (alive) timer = setTimeout(() => void load(), 3000); }
        };
        void load();
        return () => { alive = false; clearTimeout(timer); controller?.abort(); };
    }, [authenticated]);

    useEffect(() => {
        if (!authenticated || !pluginHost) { setPluginPages([]); return; }
        let mounted = true;
        let timer: ReturnType<typeof setTimeout>;
        let controller: AbortController | null = null;
        const load = async () => {
            controller = new AbortController();
            if (mounted) setPluginPagesLoading(true);
            try {
                const catalog = parsePluginCatalog(await api('/api/heredity/plugins',
                    { signal: controller.signal }));
                if (mounted) {
                    setPluginPages((previous) => mergePluginNavigation(previous, catalog.navigation));
                    setPluginPagesError(null);
                }
            } catch (failure) {
                if (mounted && !controller.signal.aborted) setPluginPagesError(message(failure));
            } finally {
                if (mounted) {
                    setPluginPagesLoading(false);
                    timer = setTimeout(() => void load(), 3000);
                }
            }
        };
        void load();
        return () => { mounted = false; clearTimeout(timer); controller?.abort(); };
    }, [authenticated, pluginHost]);

    useEffect(() => {
        if (!pluginHost || !['/', '/execution'].includes(page)) return;
        window.history.replaceState({}, '', '/plugins');
        setPage('/plugins');
    }, [pluginHost, page]);

    const navigation = NAVIGATION.filter((item) => !pluginHost
        || !['/', '/execution'].includes(item.path)).flatMap((item) =>
        item.path === '/plugins' ? [...pluginPages.map((entry) =>
            ({ path: entry.path, label: entry.title, icon: Users })), item] : [item]);
    useEffect(() => {
        document.title = `${navigation.find((item) => item.path === page)?.label ?? '控制台'} · NyXion Heredity`;
    }, [page, pluginPages]);
    const navigate = (path: string) => {
        if (!navigation.some((item) => item.path === path)) return;
        if (path === page) { setMobileOpen(false); return; }
        scrollPositions.current.set(page, scrollArea.current?.scrollTop ?? 0);
        window.history.pushState({}, '', path);
        setPage(path); setMobileOpen(false);
    };
    const logout = async () => {
        if (loggingOut) return;
        setLoggingOut(true);
        try { await post('/api/logout', {}); } catch { /* Local session still ends. */ }
        saveToken(null);
        setAuthenticated(false);
        setLoggingOut(false);
    };
    if (checking) return <main className="boot-screen"><Logo /><Loading text="正在连接管理服务" /></main>;
    if (!authenticated) return <Login onLogin={async () => {
        const status = asObject(await api('/api/status'));
        setPluginHost(status.plugin_host === true);
        setRelease((previous) => hostRelease(status, previous));
        setAuthenticated(true);
    }} />;
    const activeNav = navigation.find((item) => item.path === page) ?? navigation[0];
    const activePluginPage = pluginPages.find((item) => item.path === page);
    const waitingPluginPage = page.startsWith('/plugins/') && !activePluginPage;
    const unknownPage = !navigation.some((item) => item.path === page) && !waitingPluginPage;
    return <div className="app-shell">
        {mobileOpen && <button className="nav-backdrop" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
        <aside className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}>
            <Logo />
            <div className="sidebar-section-label">工作空间</div>
            <nav aria-label="主导航">
                {navigation.map((item) => {
                    const finding = attentionForPath(attention, item.path);
                    return <button key={item.path}
                    className={`nav-item ${activeNav.path === item.path ? 'active' : ''}`}
                    aria-label={item.label} aria-description={finding?.message}
                    title={finding?.message}
                    aria-current={activeNav.path === item.path ? 'page' : undefined} onClick={() => navigate(item.path)}>
                    <item.icon size={18} /><span>{item.label}</span>
                    {finding && <span className={`nav-attention nav-attention-${finding.severity}`}
                        aria-label={`${item.label}：${finding.count} 项${finding.severity === 'error' ? '错误' : '警告'}`}>
                        <AlertTriangle size={12} /><span>{finding.count > 99 ? '99+' : finding.count}</span>
                    </span>}
                    {activeNav.path === item.path && <ChevronRight size={13} className="nav-chevron" />}
                </button>; })}
            </nav>
            <div className="sidebar-footer"><div><span className="local-dot" />管理服务已连接</div>
                <span>NyXion Heredity <code>{release.label}</code></span></div>
        </aside>
        <div className="workspace">
            <header className="topbar">
                <Button variant="ghost" size="icon" className="mobile-menu" aria-label={mobileOpen ? '关闭导航' : '打开导航'}
                    onClick={() => setMobileOpen(!mobileOpen)}>{mobileOpen ? <X size={19} /> : <Menu size={19} />}</Button>
                <div className="breadcrumb"><span>NyXion Heredity</span><ChevronRight size={12} /><strong>{activeNav.label}</strong></div>
                <div className="topbar-end"><span className="environment-label"><Activity size={13} />本地管理</span>
                    <Button variant="ghost" size="icon" aria-label="退出登录" title="退出登录"
                        disabled={loggingOut} onClick={() => void logout()}><LogOut size={17} /></Button></div>
            </header>
            <main className="page-scroll" ref={scrollArea} aria-label={`${activeNav.label}内容`}
                onScroll={(event) => {
                    if (restoringScroll.current === null) {
                        scrollPositions.current.set(page, event.currentTarget.scrollTop);
                    }
                }}
                onWheel={() => { restoringScroll.current = null; }}
                onTouchStart={() => { restoringScroll.current = null; }}
                onKeyDown={() => { restoringScroll.current = null; }}>
                <div className="page-content" key={page} ref={pageContent}>
                {attention.filter((item) => item.path === page
                    && navigation.some((entry) => entry.path === item.path)).map((item, index) =>
                    <div key={`${item.severity}:${index}`} data-attention-severity={item.severity}
                        className={`notice ${item.severity === 'error' ? 'notice-error' : 'notice-warning'}`}
                        role={item.severity === 'error' ? 'alert' : 'status'}>
                        <AlertTriangle size={16} /><div>{item.message}</div>
                    </div>)}
                {unknownPage && <Notice error>此页面不可用</Notice>}
                {page === '/' && !pluginHost && <OverviewPage navigate={navigate} />}
                {page === '/plugins' && <PluginsPage navigate={navigate} />}
                {page === '/market' && <MarketPage hostVersion={release.version} />}
                {page === '/settings' && <SettingsPage navigate={navigate} />}
                {activePluginPage?.view === 'accounts' &&
                    <PluginAccountsPage page={activePluginPage} navigate={navigate} />}
                {waitingPluginPage && (pluginPagesLoading
                    ? <Loading text="正在读取插件页面" />
                    : <Notice error>{pluginPagesError ?? '插件未注册此页面'}</Notice>)}
                {page === '/external-connections' && <ExternalConnectionsPage />}
                {!pluginHost && page === '/execution' && <ExecutionPanel />}
                {page === '/connections' && <NetworkInterfacesPage />}
                {activeNav.path === '/debug' && <DebugPage />}
                {activeNav.path === '/logs' && <LogsPage />}
                </div>
            </main>
            <footer className="workspace-footer"><span>NyXion Heredity</span><span>本地会话 · {activeNav.label}</span></footer>
        </div>
    </div>;
}

export class AppErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
    state = { error: null as string | null };
    static getDerivedStateFromError(error: unknown) { return { error: message(error) }; }
    render() {
        if (this.state.error) return <main className="boot-screen"><Logo />
            <Notice error>{this.state.error}</Notice><Button onClick={() => window.location.reload()}>重新载入</Button></main>;
        return this.props.children;
    }
}
