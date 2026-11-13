import { useEffect, useMemo, useState } from 'react';
import { Download, RefreshCw, Search } from 'lucide-react';
import { asObject } from '../api';
import { useResource } from '../hooks';
import { Button, Empty, Loading, Notice, PageHeading } from '../ui';

interface LogLine {
    id: string | number;
    time: string;
    level: string;
    scope: string;
    message: string;
}

function parseLogs(value: unknown): LogLine[] {
    const source = asObject(value, '日志响应');
    if (!Array.isArray(source.list)) throw new Error('日志列表格式无效');
    return source.list.map((item, index) => {
        const line = asObject(item, '日志记录');
        if (typeof line.message !== 'string' || typeof line.level !== 'string') throw new Error('日志记录格式无效');
        return { id: typeof line.id === 'number' || typeof line.id === 'string' ? line.id : index,
            time: typeof line.time === 'string' ? line.time : '',
            level: line.level, scope: typeof line.scope === 'string' ? line.scope : '',
            message: line.message };
    });
}

export function LogsPage() {
    const logs = useResource('/api/logs?limit=500', parseLogs);
    const [search, setSearch] = useState('');
    const [level, setLevel] = useState('');
    const [live, setLive] = useState(false);
    useEffect(() => {
        if (!live) return;
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout>;
        const poll = async () => {
            if (!document.hidden) await logs.refresh();
            if (!cancelled) timer = setTimeout(() => void poll(), 5000);
        };
        timer = setTimeout(() => void poll(), 5000);
        return () => { cancelled = true; clearTimeout(timer); };
    }, [live, logs.refresh]);
    const filtered = useMemo(() => (logs.value ?? []).filter((line) =>
        (!level || line.level === level) && `${line.message} ${line.scope}`.toLowerCase().includes(search.toLowerCase())),
    [logs.value, level, search]);
    const download = () => {
        const text = filtered.map((line) => `${line.time} [${line.level}] [${line.scope}] ${line.message}`).join('\n');
        const link = document.createElement('a');
        const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
        link.href = url; link.download = 'heredity-visible-logs.txt'; link.click(); URL.revokeObjectURL(url);
    };
    return <>
        <PageHeading title="运行日志" subtitle="最近 500 条管理端记录"
            actions={<><Button variant="outline" size="sm" disabled={!filtered.length} onClick={download}>
                <Download size={14} />导出当前</Button>
            <Button variant="outline" size="sm" disabled={logs.loading} onClick={() => void logs.refresh()}>
                <RefreshCw size={14} className={logs.loading ? 'spin' : ''} />刷新</Button></>} />
        {logs.error && <Notice error>{logs.error}</Notice>}
        <div className="log-toolbar">
            <label className="search-field"><Search size={15} /><input value={search} placeholder="筛选日志"
                onChange={(event) => setSearch(event.target.value)} /></label>
            <select value={level} aria-label="日志级别" onChange={(event) => setLevel(event.target.value)}>
                <option value="">全部级别</option>
                {['trace', 'debug', 'info', 'success', 'warn', 'error'].map((value) =>
                    <option value={value} key={value}>{value.toUpperCase()}</option>)}
            </select>
            <label className="checkbox-label"><input type="checkbox" checked={live}
                onChange={(event) => setLive(event.target.checked)} />自动刷新</label>
            <span className="muted">{filtered.length} 条</span>
        </div>
        <section className="log-view" aria-label="运行日志">
            {!logs.value && logs.loading ? <Loading /> : filtered.length === 0 ? <Empty text="没有匹配的日志" />
                : filtered.map((line) => <div className={`log-line log-${line.level}`} key={line.id}>
                    <time>{line.time.replace('T', ' ').slice(0, 19)}</time>
                    <span className="log-level">{line.level.toUpperCase()}</span>
                    <span className="log-scope">{line.scope}</span><p>{line.message}</p>
                </div>)}
        </section>
    </>;
}
