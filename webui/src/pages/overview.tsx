import { Activity, ArrowRight, Boxes, CircleUserRound, Cpu, MemoryStick, Monitor, Plug, RefreshCw } from 'lucide-react';
import { asObject } from '../api';
import { useResource } from '../hooks';
import { bytes, duration, parseConnection } from '../models';
import { parsePluginCatalog } from '../plugin-model';
import { Button, Empty, Loading, Notice, PageHeading, Status, Badge } from '../ui';
import { parseExecutionPayload, sourceLabel } from '../../execution-model';

export function OverviewPage({ navigate }: { navigate: (page: string) => void }) {
    const system = useResource('/api/system', (value) => asObject(value, '系统信息'));
    const connection = useResource('/api/heredity/connection', parseConnection);
    const plugins = useResource('/api/heredity/plugins', parsePluginCatalog);
    const execution = useResource('/api/heredity/execution', parseExecutionPayload);
    const active = connection.value?.active;
    const descriptor = execution.value?.state.descriptor;
    const memory = system.value ? asObject(system.value.memory, '内存') : null;
    const cpu = system.value ? asObject(system.value.cpu, 'CPU') : null;
    const unavailable = Array.isArray(system.value?.metricsUnavailable)
        ? system.value.metricsUnavailable : [];
    const cpuMeasured = typeof cpu?.average === 'number'
        && !unavailable.some((item) => typeof item === 'string'
            && (item.startsWith('cpu.usage:') || item.startsWith('cpu:')));
    const refreshing = system.loading || connection.loading || plugins.loading || execution.loading;
    const refresh = () => {
        void system.refresh(); void connection.refresh(); void plugins.refresh(); void execution.refresh();
    };
    return <>
        <PageHeading title="运行概览" subtitle="当前账号、插件与执行状态"
            actions={<Button variant="outline" size="sm" disabled={refreshing} onClick={refresh}>
                <RefreshCw size={14} className={refreshing ? 'spin' : ''} />刷新</Button>} />
        {[system.error, connection.error, plugins.error, execution.error].filter(Boolean).map((error, i) =>
            <Notice error key={`${i}:${error}`}>{error}</Notice>)}
        <div className="metric-strip">
            <div><CircleUserRound size={17} /><span>当前账号</span>
                <strong>{active?.verified ? active.nickname || active.self_id : '未验证'}</strong>
                <small>{active?.verified ? String(active.self_id) : '等待接口检测'}</small></div>
            <div><Plug size={17} /><span>插件宿主</span>
                <strong>{plugins.value?.host_running ? '运行中' : '未运行'}</strong>
                <small>{plugins.value ? `${plugins.value.plugins.length} 个已注册插件` : '状态未确认'}</small></div>
            <div><Boxes size={17} /><span>活动提供方</span>
                <strong>{descriptor?.label ?? '未配置'}</strong>
                <small>{descriptor?.source_kind === 'demo' ? 'DEMO · 不属于执行等级'
                    : descriptor?.execution_level ?? '级别未分配'}</small></div>
            <div><Activity size={17} /><span>在途操作</span>
                <strong>{execution.value?.state.in_flight ?? '—'}</strong>
                <small>{execution.value?.state.state ?? '状态未确认'}</small></div>
        </div>

        <section className="section">
            <div className="section-heading"><h2>接入状态</h2>
                <Button variant="ghost" size="sm" onClick={() => navigate('/external-connections')}>
                    管理接入<ArrowRight size={14} /></Button></div>
            <div className="connection-line">
                <div className="entity-icon"><CircleUserRound size={23} /></div>
                <div className="connection-identity">
                    <div><strong>{active?.verified ? active.nickname || '已验证账号' : '尚未接通账号'}</strong>
                        {descriptor?.source_kind === 'demo' && <Badge variant="warning">DEMO</Badge>}</div>
                    <code>{active?.origin ?? (active?.kind === 'gateway' ? '本地插件通道' : '未配置活动接口')}</code>
                </div>
                <Status value={active?.verified ? active.online : null} />
            </div>
            <dl className="fact-grid">
                <div><dt>身份验证</dt><dd><Status value={active?.verified} good="已验证" bad="未验证" /></dd></div>
                <div><dt>连接来源</dt><dd>{active ? active.kind === 'gateway' ? '本地网关' : '外部接口' : '—'}</dd></div>
                <div><dt>插件账号</dt><dd>{plugins.value
                    ? `${plugins.value.sessions.filter((session) => session.state === 'online').length} 个在线` : '—'}</dd></div>
                <div><dt>执行依据</dt><dd>{descriptor ? sourceLabel(descriptor.source_kind) : '—'}</dd></div>
            </dl>
            {connection.value?.error && <Notice error>{connection.value.error}</Notice>}
        </section>

        <section className="section">
            <div className="section-heading"><h2>主机资源</h2><span className="muted">{system.value?.hostname as string ?? '—'}</span></div>
            {!system.value && system.loading ? <Loading /> : !system.value ? <Empty text="主机信息不可用" /> :
                <div className="resource-columns">
                    <div><h3><Cpu size={16} />处理器</h3>
                        <strong>{typeof cpu?.cores === 'number' ? `${cpu.cores} 核` : '—'}</strong>
                        <p>{typeof cpu?.model === 'string' ? cpu.model : '型号未提供'}</p>
                        <small>实时占用：{cpuMeasured ? `${(cpu.average as number).toFixed(1)}%` : '未测量'}</small></div>
                    <div><h3><MemoryStick size={16} />内存</h3>
                        <strong>{bytes(memory?.used)} <small>/ {bytes(memory?.total)}</small></strong>
                        <p>可用 {bytes(memory?.free)}</p>
                        {typeof memory?.usagePercent === 'number' && <progress max="100" value={memory.usagePercent} aria-label="内存使用率" />}</div>
                    <div><h3><Monitor size={16} />运行环境</h3>
                        <strong>{String(system.value.platform)} / {String(system.value.archLabel ?? system.value.arch)}</strong>
                        <p>{String(system.value.nodeVersion ?? '—')}</p>
                        <small>运行 {duration(system.value.processUptime)}</small></div>
                </div>}
        </section>
    </>;
}
