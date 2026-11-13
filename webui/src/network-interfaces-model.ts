import { asObject } from './api.ts';

export const INTERFACE_TYPES = [
    'http_server', 'http_client', 'websocket_server', 'websocket_client',
] as const;
export type InterfaceType = typeof INTERFACE_TYPES[number];
export type InterfaceState = 'stopped' | 'starting' | 'running' | 'error';
export interface InterfaceAccount {
    platform: string; self_id: string; display_name?: string;
}
export interface NetworkInterface {
    interface_id: string;
    name: string;
    type: InterfaceType;
    enabled: boolean;
    state: InterfaceState;
    error: string | null;
    account: string | null;
    settings: {
        host?: string; port?: number; url?: string; path?: string;
        heartbeat_seconds: number; sse_enabled?: boolean;
        authentication: { configured: boolean }; signing_configured: boolean;
    };
    endpoint: string | null;
    actual_port: number | null;
    modules: { module_id: string; subscriptions: number }[];
    created_at: number;
    updated_at: number;
    is_primary: boolean;
}
export interface InterfaceTypeDefinition {
    type: InterfaceType; label: string; implemented: boolean; description: string;
}
export interface NetworkInterfaceCatalog {
    schema_version: 1;
    interfaces: NetworkInterface[];
    types: InterfaceTypeDefinition[];
    accounts: InterfaceAccount[];
    module_bus: { available: boolean; id_format: 'hex16' };
}
export interface InterfaceCredentials {
    interface_id: string;
    access_token: string;
    signing_uses_access_token: true;
}
export interface InterfaceDraft {
    name: string;
    type: InterfaceType;
    account: string;
    host: string;
    port: string;
    path: string;
    url: string;
    heartbeatSeconds: string;
    sseEnabled: boolean;
}

const HEX16 = /^[0-9a-f]{16}$/;
const ACCOUNT = /^[a-z][a-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

function text(value: unknown, label: string): string {
    if (typeof value !== 'string') throw new Error(`${label}格式无效`);
    return value;
}
function flag(value: unknown, label: string): boolean {
    if (typeof value !== 'boolean') throw new Error(`${label}格式无效`);
    return value;
}
function finite(value: unknown, label: string): number {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
        throw new Error(`${label}格式无效`);
    }
    return value;
}
function integer(value: unknown, label: string, maximum = Number.MAX_SAFE_INTEGER): number {
    const result = finite(value, label);
    if (!Number.isSafeInteger(result) || result > maximum) throw new Error(`${label}格式无效`);
    return result;
}
function type(value: unknown): InterfaceType {
    if (!INTERFACE_TYPES.includes(value as InterfaceType)) throw new Error('接口类型无效');
    return value as InterfaceType;
}
function account(value: unknown): string | null {
    if (value === null) return null;
    const result = text(value, '接口账号');
    if (!ACCOUNT.test(result)) throw new Error('接口账号格式无效');
    return result;
}
function endpoint(value: unknown): string | null {
    if (value === null) return null;
    const result = text(value, '接口端点');
    let parsed: URL;
    try { parsed = new URL(result); } catch { throw new Error('接口端点无效'); }
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(parsed.protocol) || !parsed.hostname
        || parsed.username || parsed.password || parsed.hash || result.includes('\\')
        || /[^\x21-\x7e]/.test(result) || result.length > 2048) {
        throw new Error('接口端点无效');
    }
    return result;
}

export function parseNetworkInterface(value: unknown): NetworkInterface {
    const source = asObject(value, '网络接口');
    const id = text(source.interface_id, '接口 ID');
    if (!HEX16.test(id)) throw new Error('接口 ID 须为 16 位十六进制标识');
    const state = text(source.state, '接口状态');
    if (!['stopped', 'starting', 'running', 'error'].includes(state)) throw new Error('接口状态无效');
    const settings = asObject(source.settings, '接口配置');
    const authentication = asObject(settings.authentication, '接口鉴权');
    if (!Array.isArray(source.modules)) throw new Error('模块绑定列表无效');
    return {
        interface_id: id, name: text(source.name, '接口名称'), type: type(source.type),
        enabled: flag(source.enabled, '接口开关'), state: state as InterfaceState,
        error: source.error === null ? null : text(source.error, '接口错误'),
        account: account(source.account),
        settings: {
            ...(settings.host === undefined ? {} : { host: text(settings.host, '绑定地址') }),
            ...(settings.port === undefined ? {} : { port: integer(settings.port, '绑定端口', 65535) }),
            ...(settings.path === undefined ? {} : { path: text(settings.path, '接口路径') }),
            ...(settings.url === undefined ? {} : { url: settings.url === '' ? ''
                : endpoint(settings.url) ?? undefined }),
            heartbeat_seconds: finite(settings.heartbeat_seconds, '接口心跳'),
            ...(settings.sse_enabled === undefined ? {}
                : { sse_enabled: flag(settings.sse_enabled, 'SSE 开关') }),
            authentication: { configured: flag(authentication.configured, '接口鉴权') },
            signing_configured: flag(settings.signing_configured, '事件签名'),
        },
        endpoint: endpoint(source.endpoint),
        actual_port: source.actual_port === null ? null : integer(source.actual_port, '实际端口', 65535),
        modules: source.modules.map((raw) => {
            const entry = asObject(raw, '模块绑定');
            return { module_id: text(entry.module_id, '模块 ID'),
                subscriptions: integer(entry.subscriptions, '模块订阅数') };
        }),
        created_at: finite(source.created_at, '创建时间'), updated_at: finite(source.updated_at, '更新时间'),
        is_primary: source.is_primary === undefined ? false : flag(source.is_primary, '主接口标识'),
    };
}

export function parseNetworkInterfaces(value: unknown): NetworkInterfaceCatalog {
    const source = asObject(value, '网络接口目录');
    if (source.schema_version !== 1 || !Array.isArray(source.interfaces)
        || !Array.isArray(source.types) || !Array.isArray(source.accounts)) {
        throw new Error('网络接口目录版本或格式无效');
    }
    const interfaces = source.interfaces.map(parseNetworkInterface);
    if (new Set(interfaces.map((item) => item.interface_id)).size !== interfaces.length) {
        throw new Error('网络接口目录包含重复 ID');
    }
    const types = source.types.map((raw) => {
        const entry = asObject(raw, '接口类型');
        return { type: type(entry.type), label: text(entry.label, '类型名称'),
            implemented: flag(entry.implemented, '类型可用状态'), description: text(entry.description, '类型说明') };
    });
    if (new Set(types.map((entry) => entry.type)).size !== types.length) throw new Error('接口类型重复');
    const bus = asObject(source.module_bus, '模块总线');
    if (bus.id_format !== 'hex16') throw new Error('模块接口 ID 格式不兼容');
    return {
        schema_version: 1, interfaces, types,
        accounts: source.accounts.map((raw) => {
            const entry = asObject(raw, '接口账号');
            const platform = text(entry.platform, '平台');
            const self_id = text(entry.self_id, '账号');
            account(`${platform}:${self_id}`);
            return { platform, self_id,
                ...(entry.display_name === undefined ? {} : { display_name: text(entry.display_name, '账号名称') }) };
        }),
        module_bus: { available: flag(bus.available, '模块总线状态'), id_format: 'hex16' },
    };
}

export function interfaceDraft(source?: NetworkInterface): InterfaceDraft {
    return {
        name: source?.name ?? '', type: source?.type ?? 'http_server', account: source?.account ?? '',
        host: source?.settings.host ?? '127.0.0.1', port: String(source?.settings.port ?? 0),
        path: source?.settings.path ?? '/', url: source?.settings.url ?? '',
        heartbeatSeconds: String(source?.settings.heartbeat_seconds ?? 15),
        sseEnabled: source?.settings.sse_enabled ?? true,
    };
}

export function isServerType(value: InterfaceType) {
    return value === 'http_server' || value === 'websocket_server';
}

export function interfacePayload(draft: InterfaceDraft, creating: boolean) {
    if (typeof draft.name !== 'string' || !draft.name.trim() || draft.name.length > 80
        || /[\u0000-\u001f\u007f]/.test(draft.name)) throw new Error('接口名称须为 1–80 个可见字符');
    type(draft.type);
    const selected = draft.account.trim() ? account(draft.account.trim()) : null;
    const heartbeat = Number(draft.heartbeatSeconds);
    if (!draft.heartbeatSeconds.trim() || !Number.isFinite(heartbeat) || heartbeat < 1 || heartbeat > 86400) {
        throw new Error('心跳间隔须在 1–86400 秒之间');
    }
    const settings: Record<string, unknown> = { heartbeat_seconds: heartbeat };
    if (isServerType(draft.type)) {
        if (!draft.host.trim() || draft.host.length > 255 || /[\u0000-\u0020\u007f/\\]/.test(draft.host)) {
            throw new Error('请填写有效的监听地址');
        }
        const port = Number(draft.port);
        if (!draft.port.trim() || !Number.isInteger(port) || port < 0 || port > 65535) {
            throw new Error('监听端口须为 0–65535');
        }
        if (!draft.path.startsWith('/') || draft.path.startsWith('//') || draft.path.length > 256
            || /[\u0000-\u0020\u007f?#%\\]/.test(draft.path)
            || draft.path.split('/').some((part) => part === '.' || part === '..')) {
            throw new Error('请填写以 / 开始的本地接口路径');
        }
        Object.assign(settings, { host: draft.host.trim(), port, path: draft.path });
        if (draft.type === 'http_server') settings.sse_enabled = draft.sseEnabled;
    } else {
        const address = endpoint(draft.url);
        if (!address) throw new Error('请填写远端接口地址');
        const protocol = new URL(address).protocol;
        if (new URL(address).search) throw new Error('远端接口地址不能包含查询参数');
        if (draft.type === 'http_client' && !['http:', 'https:'].includes(protocol)) {
            throw new Error('HTTP 客户端须使用 HTTP(S) 地址');
        }
        if (draft.type === 'websocket_client' && !['ws:', 'wss:'].includes(protocol)) {
            throw new Error('WebSocket 客户端须使用 WS(S) 地址');
        }
        settings.url = address;
    }
    return { name: draft.name.trim(), ...(creating ? { type: draft.type, enabled: false } : {}),
        account: selected, settings };
}

export function parseInterfaceCredentials(value: unknown, expectedId: string): InterfaceCredentials {
    interfacePath(expectedId);
    const source = asObject(value, '接口凭据');
    const token = text(source.access_token, '接口令牌');
    if (source.interface_id !== expectedId || source.signing_uses_access_token !== true
        || !token || token.length > 4096 || /[^\x21-\x7e]/.test(token)) {
        throw new Error('接口凭据与所选接口不一致或格式无效');
    }
    return { interface_id: expectedId, access_token: token, signing_uses_access_token: true };
}

export function parseRegeneratedCredentials(value: unknown, expectedId: string): InterfaceCredentials {
    const source = asObject(value, '接口令牌生成响应');
    const item = parseNetworkInterface(source.interface);
    if (item.interface_id !== expectedId) throw new Error('接口令牌生成响应的接口 ID 不一致');
    return parseInterfaceCredentials({ ...source, signing_uses_access_token: true }, expectedId);
}

export function interfacePath(id: string) {
    if (!HEX16.test(id)) throw new Error('接口 ID 无效');
    return `/api/heredity/network-interfaces/${id}`;
}

export const INTERFACE_STATE_LABELS: Record<InterfaceState, string> = {
    stopped: '已停止', starting: '启动中', running: '运行中', error: '错误',
};
