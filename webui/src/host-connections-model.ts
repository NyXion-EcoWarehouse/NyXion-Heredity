import { asObject } from './api.ts';

export interface HostAccount {
    platform: string;
    self_id: string;
    plugin_id?: string;
    instance_key?: string;
    display_name?: string;
}
export interface HostConnections {
    schema_version: 1;
    http: {
        enabled: boolean; origin: string; host: string; port: number;
        authentication: { configured: boolean };
    };
    sse: { enabled: boolean; path: string; url: string };
    event_delivery: { urls: string[]; heartbeat_seconds: number; signing_configured: boolean };
    account_selection: { account: string | null; accounts: HostAccount[]; state: string };
    supported_transports: string[];
    unsupported_transports: string[];
    restart_required: boolean;
}
export type SecretMode = 'keep' | 'replace' | 'clear';
export interface HostConnectionDraft {
    eventUrls: string[];
    sseEnabled: boolean;
    accessTokenMode: SecretMode;
    accessToken: string;
    eventSecretMode: SecretMode;
    eventSecret: string;
}

function text(value: unknown, name: string) {
    if (typeof value !== 'string') throw new Error(`${name}格式无效`);
    return value;
}
function flag(value: unknown, name: string) {
    if (typeof value !== 'boolean') throw new Error(`${name}格式无效`);
    return value;
}
function finite(value: unknown, name: string) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
        throw new Error(`${name}格式无效`);
    }
    return value;
}
function strings(value: unknown, name: string) {
    if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
        throw new Error(`${name}格式无效`);
    }
    return value as string[];
}
function httpUrl(value: unknown, name: string) {
    const result = text(value, name);
    let parsed: URL;
    try { parsed = new URL(result); } catch { throw new Error(`${name}须为完整 HTTP(S) 地址`); }
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname
        || parsed.username || parsed.password || parsed.hash || result.includes('#')
        || result.includes('\\') || /[^\x21-\x7e]/.test(result) || result.length > 2048) {
        throw new Error(`${name}须为不含凭据和片段的 HTTP(S) 地址`);
    }
    return result;
}

export function parseHostConnections(value: unknown): HostConnections {
    const source = asObject(value, '宿主连接');
    if (source.schema_version !== 1) throw new Error('宿主连接版本不兼容');
    const http = asObject(source.http, 'HTTP 监听');
    const authentication = asObject(http.authentication, '接口鉴权');
    const sse = asObject(source.sse, 'SSE 通道');
    const delivery = asObject(source.event_delivery, '事件投递');
    const selection = asObject(source.account_selection, '账号选择');
    const port = finite(http.port, '监听端口');
    if (!Number.isInteger(port) || port > 65535) throw new Error('监听端口格式无效');
    const origin = httpUrl(http.origin, 'HTTP 地址');
    const parsedOrigin = new URL(origin);
    if (parsedOrigin.pathname !== '/' || parsedOrigin.search) throw new Error('HTTP 地址须为 origin');
    const path = text(sse.path, 'SSE 路径');
    if (path !== '/_heredity/events') throw new Error('SSE 路径不兼容');
    const sseUrl = httpUrl(sse.url, 'SSE 地址');
    if (new URL(sseUrl).origin !== parsedOrigin.origin || new URL(sseUrl).pathname !== path
        || new URL(sseUrl).search) throw new Error('SSE 地址与宿主监听不一致');
    if (!Array.isArray(selection.accounts)) throw new Error('在线账号列表格式无效');
    const accounts = selection.accounts.map((raw) => {
        const entry = asObject(raw, '在线账号');
        return {
            platform: text(entry.platform, '账号平台'), self_id: text(entry.self_id, '账号标识'),
            ...(entry.plugin_id == null ? {} : { plugin_id: text(entry.plugin_id, '插件标识') }),
            ...(entry.instance_key == null ? {} : { instance_key: text(entry.instance_key, '账号位') }),
            ...(entry.display_name == null ? {} : { display_name: text(entry.display_name, '账号名称') }),
        };
    });
    return {
        schema_version: 1,
        http: { enabled: flag(http.enabled, 'HTTP 监听状态'), origin,
            host: text(http.host, '绑定地址'), port,
            authentication: { configured: flag(authentication.configured, '鉴权配置') } },
        sse: { enabled: flag(sse.enabled, 'SSE 状态'), path, url: sseUrl },
        event_delivery: {
            urls: strings(delivery.urls, '事件接收地址').map((url) => httpUrl(url, '事件接收地址')),
            heartbeat_seconds: finite(delivery.heartbeat_seconds, '事件心跳间隔'),
            signing_configured: flag(delivery.signing_configured, '事件签名配置'),
        },
        account_selection: {
            account: selection.account == null ? null : text(selection.account, '当前账号'),
            accounts, state: text(selection.state, '账号路由状态'),
        },
        supported_transports: strings(source.supported_transports, '可用传输'),
        unsupported_transports: strings(source.unsupported_transports, '未实现传输'),
        restart_required: flag(source.restart_required, '重启状态'),
    };
}

export function hostConnectionDraft(settings: HostConnections): HostConnectionDraft {
    return { eventUrls: [...settings.event_delivery.urls], sseEnabled: settings.sse.enabled,
        accessTokenMode: 'keep', accessToken: '', eventSecretMode: 'keep', eventSecret: '' };
}

export function hostConnectionPayload(draft: HostConnectionDraft) {
    if (!Array.isArray(draft.eventUrls) || draft.eventUrls.length > 100) {
        throw new Error('事件接收地址最多 100 个');
    }
    const urls = draft.eventUrls.map((raw) => {
        if (typeof raw !== 'string' || /[\u0000-\u001f\u007f]/.test(raw)) {
            throw new Error('事件接收地址不能包含控制字符');
        }
        return httpUrl(raw.trim(), '事件接收地址');
    });
    if (new Set(urls).size !== urls.length) throw new Error('事件接收地址不能重复');
    if (typeof draft.sseEnabled !== 'boolean') throw new Error('SSE 状态须为开关值');
    const payload: Record<string, unknown> = { event_urls: urls, sse_enabled: draft.sseEnabled };
    for (const [key, mode, value] of [
        ['access_token', draft.accessTokenMode, draft.accessToken],
        ['event_secret', draft.eventSecretMode, draft.eventSecret],
    ] as const) {
        if (!['keep', 'replace', 'clear'].includes(mode)) throw new Error('密钥操作无效');
        if (mode === 'keep') continue;
        if (mode === 'clear') { payload[key] = ''; continue; }
        if (typeof value !== 'string' || !value || value.length > 4096
            || /[\u0000-\u0020\u007f]/.test(value)) {
            throw new Error(key === 'access_token' ? '请填写有效的接口令牌' : '请填写有效的事件签名密钥');
        }
        payload[key] = value;
    }
    return payload;
}
