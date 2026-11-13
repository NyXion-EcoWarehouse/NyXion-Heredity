import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Copy, Loader2, Play, Send } from 'lucide-react';
import { message, post } from '../api';
import { useResource } from '../hooks';
import { parseAccounts, parseParams, READ_ACTIONS } from '../models';
import { Badge, Button, Notice, PageHeading } from '../ui';

const ACTIONS = [
    ['get_status', '运行状态'], ['get_version_info', '版本信息'], ['get_runtime_info', '账号运行时'],
    ['get_login_info', '账号资料'], ['get_friend_list', '好友列表'], ['get_group_list', '群列表'],
    ['get_msg', '本地消息'], ['get_local_history', '本地历史'],
    ['send_private_msg', '发送私聊'], ['send_group_msg', '发送群聊'], ['delete_msg', '撤回消息'],
];

export function DebugPage() {
    const list = useResource('/api/accounts', parseAccounts);
    const [account, setAccount] = useState('');
    const [action, setAction] = useState('get_status');
    const [params, setParams] = useState('{}');
    const [confirmed, setConfirmed] = useState(false);
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<unknown>(null);
    const [error, setError] = useState<string | null>(null);
    const [elapsed, setElapsed] = useState<number | null>(null);
    const sending = useRef(false);
    useEffect(() => {
        if (list.value && !list.value.some((item) => item.account === account)) {
            setAccount(list.value[0]?.account ?? '');
            setConfirmed(false);
        }
    }, [list.value, account]);
    const readOnly = READ_ACTIONS.has(action);
    const isSend = action.startsWith('send_');
    const invoke = async () => {
        if (sending.current || !account || (!readOnly && !confirmed)) return;
        let parsed: Record<string, unknown>;
        try { parsed = parseParams(params); } catch (failure) { setError(message(failure)); return; }
        sending.current = true;
        setBusy(true); setError(null); setElapsed(null);
        const started = performance.now();
        try {
            const value = await post('/api/debug/invoke', { account, action, params: parsed });
            setResult(value);
            setElapsed(Math.round(performance.now() - started));
        } catch (failure) {
            setError(message(failure));
        } finally {
            sending.current = false;
            setBusy(false);
            setConfirmed(false);
        }
    };
    return <>
        <PageHeading title="OneBot 调试" subtitle="动作仅在手动执行后发出" />
        {list.error && <Notice error>{list.error}</Notice>}
        {error && <Notice error>{error}</Notice>}
        <section className="section">
            <div className="section-heading"><h2>动作请求</h2>
                <Badge variant={readOnly ? 'secondary' : 'warning'}>{readOnly ? '读取动作' : '写操作'}</Badge></div>
            <div className="debug-fields">
                <label className="field">账号<select value={account} onChange={(event) => { setAccount(event.target.value); setConfirmed(false); }} disabled={busy}>
                    {!list.value?.length && <option value="">无已验证账号</option>}
                    {list.value?.map((item) => <option value={item.account} key={item.account}>
                        {item.nickname || item.self_id} · {item.account}
                    </option>)}
                </select></label>
                <label className="field">动作<select value={action} disabled={busy}
                    onChange={(event) => { setAction(event.target.value); setConfirmed(false); }}>
                    {ACTIONS.map(([name, label]) => <option value={name} key={name}>{label} · {name}</option>)}
                </select></label>
            </div>
            <label className="field code-field">参数 JSON
                <textarea value={params} onChange={(event) => { setParams(event.target.value); setConfirmed(false); }}
                    spellCheck={false} disabled={busy} rows={10} /></label>
            {!readOnly && <div className="write-confirm">
                <AlertTriangle size={17} />
                <label><input type="checkbox" checked={confirmed} disabled={busy}
                    onChange={(event) => setConfirmed(event.target.checked)} />
                    我确认对账号 {account || '未选择'} 执行{isSend ? '消息发送' : '写操作'}。</label>
            </div>}
            <div className="form-footer"><Button disabled={busy || !account || (!readOnly && !confirmed)} onClick={() => void invoke()}>
                {busy ? <Loader2 size={15} className="spin" /> : isSend ? <Send size={15} /> : <Play size={15} />}
                {busy ? '执行中' : readOnly ? '执行读取' : isSend ? '确认发送' : '确认执行写操作'}
            </Button></div>
        </section>
        <section className="section">
            <div className="section-heading"><h2>响应</h2><div className="inline-controls">
                {elapsed !== null && <span className="muted">{elapsed} ms</span>}
                <Button variant="ghost" size="icon" disabled={result === null} aria-label="复制响应" title="复制响应"
                    onClick={() => void navigator.clipboard.writeText(JSON.stringify(result, null, 2)).catch((failure) => setError(message(failure)))}>
                    <Copy size={15} /></Button></div></div>
            {result === null ? <p className="section-note">尚未执行动作。</p>
                : <pre className="json-output">{JSON.stringify(result, null, 2)}</pre>}
        </section>
    </>;
}
