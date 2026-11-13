import { asObject } from './api.ts';

export interface HostRelease {
    version: string;
    label: string;
}

export function hostRelease(value: unknown, fallback: HostRelease = {
    version: '26.11.13.post1', label: 'Alpha Build 26.11.13 (Core Revision 1)',
}): HostRelease {
    const source = asObject(value, '宿主版本');
    const valid = (entry: unknown): entry is string => typeof entry === 'string'
        && entry.trim().length > 0 && entry.length <= 160 && !/[\u0000-\u001f\u007f]/.test(entry);
    const version = valid(source.version) ? source.version.trim() : fallback.version;
    const label = valid(source.version_label) ? source.version_label.trim()
        : valid(source.version) ? (/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version)
            ? `v${version}` : version) : fallback.label;
    return { version, label };
}

export interface AccountSnapshot {
    account: string;
    platform: string;
    self_id: string;
    nickname: string;
}

interface ConnectionIdentity {
    self_id: number;
    nickname: string;
    online: boolean;
    verified: boolean;
    execution_level: string | null;
    provider_id: string | null;
    event_ingress?: EventIngress;
}

export type ActiveConnection = ConnectionIdentity & (
    { kind: 'gateway'; origin: string | null; gateway_origin?: string | null }
    | { kind: 'external'; origin: string; gateway_origin?: string }
);

export interface EventIngress {
    enabled: boolean;
    healthy: boolean | null;
    last_event_at: number | null;
    accepted: number;
    duplicates: number;
    reason: string;
}

export interface ConnectionSnapshot {
    active: ActiveConnection | null;
    configured: boolean;
    error?: string;
    event_ingress?: EventIngress;
}

export interface ConnectionProbe {
    probe_id: string;
    origin: string;
    self_id: number;
    nickname: string;
    online: boolean;
    execution_level: string | null;
    expires_at: number;
}

function string(value: unknown, name: string): string {
    if (typeof value !== 'string') throw new Error(`${name}格式无效`);
    return value;
}

function boolean(value: unknown, name: string): boolean {
    if (typeof value !== 'boolean') throw new Error(`${name}格式无效`);
    return value;
}

function integer(value: unknown, name: string): number {
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
        throw new Error(`${name}格式无效`);
    }
    return value;
}

function timestamp(value: unknown, name: string): number {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
        throw new Error(`${name}格式无效`);
    }
    return value;
}

function list(value: unknown, name: string): unknown[] {
    if (!Array.isArray(value)) throw new Error(`${name}格式无效`);
    return value;
}

export function parseAccounts(value: unknown): AccountSnapshot[] {
    const source = asObject(value, '账号列表');
    const accounts = list(source.list, '账号列表').map((raw) => {
        const item = asObject(raw, '账号');
        const platform = string(item.platform, '账号平台');
        const self_id = string(item.self_id, '账号标识');
        const account = string(item.account, '账号身份');
        const nickname = string(item.nickname, '账号昵称');
        if (!/^[a-z][a-z0-9_.-]{0,127}$/.test(platform)
            || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(self_id)
            || account !== `${platform}:${self_id}`
            || nickname.length > 1024 || /[\u0000-\u001f\u007f]/.test(nickname)) {
            throw new Error('账号资料格式无效');
        }
        return { account, platform, self_id, nickname };
    });
    if (new Set(accounts.map((item) => item.account)).size !== accounts.length) {
        throw new Error('账号列表包含重复身份');
    }
    return accounts;
}

export function parseConnection(value: unknown): ConnectionSnapshot {
    const source = asObject(value, '账号连接');
    let active: ActiveConnection | null = null;
    if (source.active !== null) {
        const item = asObject(source.active, '活动连接');
        if (item.kind !== 'gateway' && item.kind !== 'external') throw new Error('连接类型格式无效');
        const identity: ConnectionIdentity = {
            self_id: integer(item.self_id, '账号'), nickname: string(item.nickname, '昵称'),
            online: boolean(item.online, '在线状态'), verified: boolean(item.verified, '身份验证'),
            execution_level: item.execution_level == null ? null : string(item.execution_level, '执行级别'),
            provider_id: item.provider_id == null ? null : string(item.provider_id, '提供方'),
            ...(item.event_ingress !== undefined ? { event_ingress: parseEventIngress(item.event_ingress) } : {}),
        };
        active = item.kind === 'gateway' ? {
            ...identity, kind: 'gateway',
            origin: item.origin === null ? null : connectionOrigin(item.origin, '接口地址'),
            ...(item.gateway_origin === undefined ? {} : {
                gateway_origin: item.gateway_origin === null
                    ? null : connectionOrigin(item.gateway_origin, '网关地址'),
            }),
        } : {
            ...identity, kind: 'external', origin: connectionOrigin(item.origin, '接口地址'),
            ...(item.gateway_origin === undefined ? {} : {
                gateway_origin: connectionOrigin(item.gateway_origin, '网关地址'),
            }),
        };
    }
    const eventIngress = source.event_ingress === undefined ? active?.event_ingress
        : parseEventIngress(source.event_ingress);
    return { active, configured: boolean(source.configured, '配置状态'),
        ...(typeof source.error === 'string' ? { error: source.error } : {}),
        ...(eventIngress ? { event_ingress: eventIngress } : {}) };
}

function connectionOrigin(value: unknown, name: string): string {
    const result = string(value, name);
    let url: URL;
    try { url = new URL(result); } catch { throw new Error(`${name}格式无效`); }
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname
        || url.username || url.password || !['', '/'].includes(url.pathname)
        || url.search || url.hash || result.includes('\\') || result.length > 2048
        || /[\u0000-\u0020\u007f]/.test(result)) throw new Error(`${name}格式无效`);
    return result;
}

function parseEventIngress(value: unknown): EventIngress {
    const item = asObject(value, '入站事件状态');
    return {
        enabled: boolean(item.enabled, '入站事件状态'),
        healthy: item.healthy == null ? null : boolean(item.healthy, '事件接收健康'),
        last_event_at: item.last_event_at == null ? null : timestamp(item.last_event_at, '最近事件时间'),
        accepted: item.accepted == null ? 0 : integer(item.accepted, '事件数'),
        duplicates: item.duplicates == null ? 0 : integer(item.duplicates, '重复事件数'),
        reason: string(item.reason, '入站事件说明'),
    };
}

export function parseProbe(value: unknown): ConnectionProbe {
    const source = asObject(value, '接口检测');
    return {
        probe_id: string(source.probe_id, '检测标识'), origin: string(source.origin, '接口地址'),
        self_id: integer(source.self_id, '账号'), nickname: string(source.nickname, '昵称'),
        online: boolean(source.online, '在线状态'),
        execution_level: source.execution_level == null ? null : string(source.execution_level, '执行级别'),
        expires_at: timestamp(source.expires_at, '检测有效期'),
    };
}

export function localOrigin(value: string): string {
    let url: URL;
    try {
        url = new URL(value.trim());
    } catch {
        throw new Error('请输入有效的本机 HTTP 或 HTTPS 地址');
    }
    const host = url.hostname.toLowerCase();
    const local = host === 'localhost' || host === '[::1]'
        || /^127(?:\.\d{1,3}){3}$/.test(host);
    if (!['http:', 'https:'].includes(url.protocol) || !local || !url.port
        || url.username || url.password || !['', '/'].includes(url.pathname)
        || url.search || url.hash) {
        throw new Error('地址须为带端口的本机 HTTP(S) origin，不能包含路径或凭据');
    }
    return url.origin;
}

export const READ_ACTIONS = new Set([
    'get_status', 'get_version_info', 'get_runtime_info', 'get_login_info',
    'get_friend_list', 'get_group_list', 'get_msg', 'get_local_history',
    'can_send_image', 'can_send_record',
]);

export function parseParams(source: string): Record<string, unknown> {
    let result: unknown;
    try {
        result = JSON.parse(source);
    } catch {
        throw new Error('参数必须是有效的 JSON 对象');
    }
    return asObject(result, '参数');
}

export function bytes(value: unknown): string {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—';
    if (value === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    return `${(value / 1024 ** index).toFixed(index > 0 ? 1 : 0)} ${units[index]}`;
}

export function duration(value: unknown): string {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—';
    const seconds = Math.floor(value);
    if (seconds >= 86400) return `${Math.floor(seconds / 86400)} 天 ${Math.floor(seconds % 86400 / 3600)} 小时`;
    if (seconds >= 3600) return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
    return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

