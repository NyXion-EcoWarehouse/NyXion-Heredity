import { asObject } from './api.ts';

export type PluginState = 'discovered' | 'handshaking' | 'starting' | 'running'
    | 'stopping' | 'stopped' | 'failed';
export interface PluginSession {
    platform: string;
    self_id: string | null;
    session_epoch: string | null;
    state: 'starting' | 'login_required' | 'authenticating' | 'online' | 'offline' | 'failed' | 'stopped';
    reason: string;
}
export interface PluginInstanceSnapshot {
    id: string;
    name: string;
    version: string;
    platform: string;
    execution: 'embedded';
    state: PluginState;
    handshake: { api_major: number; api_minor: number; verified: boolean };
    heartbeat: { sequence: number; last_seen_at: number | null; lease_seconds: number; healthy: boolean };
    session: PluginSession | null;
    actions: string[];
    capabilities: string[];
    config_schema: Record<string, unknown>;
    config_values: Record<string, string | number | boolean>;
    has_saved_config: boolean;
    error: string | null;
    instance_key: string;
    instance_id: string | null;
    display_name: string;
    is_default: boolean;
}
export interface PluginPage {
    id: string;
    title: string;
    path: string;
    view: 'accounts';
    icon: 'users';
}
export interface PluginNavigation extends PluginPage {
    plugin_id: string;
}

export function mergePluginNavigation(previous: PluginNavigation[], incoming: PluginNavigation[]) {
    const current = new Map(incoming.map((entry) => [entry.path, entry]));
    const retained = previous.flatMap((entry) => {
        const updated = current.get(entry.path);
        if (!updated) return [];
        current.delete(entry.path);
        return [updated];
    });
    return [...retained, ...current.values()];
}

export interface PluginSnapshot extends PluginInstanceSnapshot {
    instances: PluginInstanceSnapshot[];
    ui_pages: PluginPage[];
}
export interface PluginCatalog {
    api_version: '1.0';
    host_running: boolean;
    plugins: PluginSnapshot[];
    sessions: PluginSession[];
    navigation: PluginNavigation[];
}
export interface PluginChallenge {
    challenge_id: string;
    kind: 'qr' | 'notice' | 'text' | 'input';
    title: string;
    expires_at: number | null;
    message: string;
    image: string | null;
}
export interface AccountSelection {
    account: string | null;
    accounts: { platform: string; self_id: string }[];
    state: string;
}
export interface ConfigField {
    key: string;
    title: string;
    description: string;
    type: 'string' | 'integer' | 'number' | 'boolean';
    required: boolean;
    secret: boolean;
    defaultValue: string | boolean;
    choices: (string | number | boolean)[];
    minimum?: number;
    maximum?: number;
}

function text(value: unknown, name: string): string {
    if (typeof value !== 'string') throw new Error(`${name}格式无效`);
    return value;
}
function flag(value: unknown, name: string): boolean {
    if (typeof value !== 'boolean') throw new Error(`${name}格式无效`);
    return value;
}
function finite(value: unknown, name: string): number {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
        throw new Error(`${name}格式无效`);
    }
    return value;
}
function integer(value: unknown, name: string): number {
    const result = finite(value, name);
    if (!Number.isSafeInteger(result)) throw new Error(`${name}格式无效`);
    return result;
}
function strings(value: unknown, name: string): string[] {
    if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
        throw new Error(`${name}格式无效`);
    }
    return value as string[];
}

export function parsePluginSession(value: unknown): PluginSession {
    const source = asObject(value, '插件会话');
    const state = text(source.state, '账号状态');
    if (!['starting', 'login_required', 'authenticating', 'online', 'offline', 'failed', 'stopped'].includes(state)) {
        throw new Error('账号状态格式无效');
    }
    return {
        platform: text(source.platform, '账号平台'),
        self_id: source.self_id == null ? null : text(source.self_id, '账号标识'),
        session_epoch: source.session_epoch == null ? null : text(source.session_epoch, '会话代次'),
        state: state as PluginSession['state'],
        reason: source.reason == null ? '' : text(source.reason, '会话说明'),
    };
}

function parsePluginInstance(value: unknown): PluginInstanceSnapshot {
    const source = asObject(value, '插件');
    const state = text(source.state, '插件状态');
    if (!['discovered', 'handshaking', 'starting', 'running', 'stopping', 'stopped', 'failed'].includes(state)
        || source.execution !== 'embedded') throw new Error('插件执行状态格式无效');
    const handshake = asObject(source.handshake, '握手状态');
    const heartbeat = asObject(source.heartbeat, '握手心跳');
    const config_schema = asObject(source.config_schema, '配置契约');
    return {
        id: text(source.id, '插件标识'), name: text(source.name, '插件名称'),
        version: text(source.version, '插件版本'), platform: text(source.platform, '平台'),
        execution: 'embedded', state: state as PluginState,
        handshake: { api_major: integer(handshake.api_major, 'SDK 主版本'),
            api_minor: integer(handshake.api_minor, 'SDK 次版本'), verified: flag(handshake.verified, '握手验证') },
        heartbeat: { sequence: integer(heartbeat.sequence, '握手序号'),
            last_seen_at: heartbeat.last_seen_at == null ? null : finite(heartbeat.last_seen_at, '最近握手'),
            lease_seconds: finite(heartbeat.lease_seconds, '握手租期'), healthy: flag(heartbeat.healthy, '握手健康') },
        session: source.session == null ? null : parsePluginSession(source.session),
        actions: strings(source.actions, '动作能力'), capabilities: strings(source.capabilities, '能力列表'),
        config_schema, config_values: savedConfig(source.config_values, config_schema),
        has_saved_config: source.has_saved_config === undefined ? false
            : flag(source.has_saved_config, '已保存配置'),
        error: source.error == null ? null : text(source.error, '插件错误'),
        instance_key: source.instance_key == null ? 'default' : instanceKey(source.instance_key),
        instance_id: source.instance_id == null ? null : text(source.instance_id, '运行实例标识'),
        display_name: source.display_name == null ? text(source.name, '插件名称')
            : text(source.display_name, '账号名称'),
        is_default: source.is_default == null ? true : flag(source.is_default, '默认实例'),
    };
}

function savedConfig(value: unknown, schema: Record<string, unknown>) {
    if (value === undefined) return {};
    const source = asObject(value, '已保存插件配置');
    const properties = asObject(schema.properties ?? {}, '配置字段');
    const output: Record<string, string | number | boolean> = {};
    for (const [key, item] of Object.entries(source)) {
        const field = asObject(properties[key], '已保存配置字段');
        if (field.writeOnly === true || field.format === 'password') continue;
        if ((field.type === 'string' && typeof item === 'string')
            || (field.type === 'boolean' && typeof item === 'boolean')
            || (['integer', 'number'].includes(String(field.type)) && typeof item === 'number'
                && Number.isFinite(item) && (field.type !== 'integer' || Number.isSafeInteger(item)))) {
            output[key] = item as string | number | boolean;
        } else throw new Error(`已保存配置字段 ${key} 格式无效`);
    }
    return output;
}

export function configInputValues(values: Record<string, string | number | boolean>) {
    return Object.fromEntries(Object.entries(values).map(([key, value]) =>
        [key, typeof value === 'boolean' ? value : String(value)]));
}

function instanceKey(value: unknown): string {
    if (typeof value !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(value)) {
        throw new Error('账号实例标识无效');
    }
    return value;
}

export function pluginInstancePath(pluginId: string, key: string): string {
    if (!/^[a-z][a-z0-9_.-]{0,127}$/.test(pluginId)) throw new Error('插件标识无效');
    return `/api/heredity/plugins/${encodeURIComponent(pluginId)}/instances/${encodeURIComponent(instanceKey(key))}`;
}

function parsePluginPage(value: unknown, pluginId: string): PluginPage {
    const source = asObject(value, '插件页面');
    const id = text(source.id, '插件页面标识');
    if (!/^[a-z][a-z0-9_.-]{0,127}$/.test(pluginId)
        || !/^[a-z][a-z0-9-]{0,63}$/.test(id)) throw new Error('插件页面注册无效');
    const expectedPath = `/plugins/${pluginId}/${id}`;
    if (source.view !== 'accounts' || source.icon !== 'users'
        || (source.path !== undefined && source.path !== expectedPath)) {
        throw new Error('插件页面注册无效');
    }
    return { id, title: text(source.title, '插件页面标题'), path: expectedPath,
        view: 'accounts', icon: 'users' };
}

export function parsePlugin(value: unknown): PluginSnapshot {
    const source = asObject(value, '插件');
    const base = parsePluginInstance(source);
    if (source.instances !== undefined && !Array.isArray(source.instances)) {
        throw new Error('插件账号列表无效');
    }
    const instances = source.instances === undefined ? [base]
        : source.instances.map(parsePluginInstance);
    if (instances.some((instance) => instance.id !== base.id || instance.platform !== base.platform)
        || new Set(instances.map((instance) => instance.instance_key)).size !== instances.length) {
        throw new Error('插件账号实例冲突');
    }
    if (source.ui_pages !== undefined && !Array.isArray(source.ui_pages)) {
        throw new Error('插件页面列表无效');
    }
    const pages = (source.ui_pages ?? []).map((page: unknown) => parsePluginPage(page, base.id));
    if (new Set(pages.map((page: PluginPage) => page.id)).size !== pages.length) {
        throw new Error('插件页面重复注册');
    }
    return { ...base, instances, ui_pages: pages };
}

export function parsePluginCatalog(value: unknown): PluginCatalog {
    const source = asObject(value, '插件目录');
    if (source.api_version !== '1.0' || !Array.isArray(source.plugins) || !Array.isArray(source.sessions)) {
        throw new Error('插件目录版本或格式无效');
    }
    const plugins = source.plugins.map(parsePlugin);
    if (new Set(plugins.map((plugin) => plugin.id)).size !== plugins.length) {
        throw new Error('插件目录包含重复标识');
    }
    const declaredPages = plugins.flatMap((plugin) => plugin.ui_pages.map((page) =>
        ({ ...page, plugin_id: plugin.id })));
    let navigation = declaredPages;
    if (source.navigation !== undefined) {
        if (!Array.isArray(source.navigation)) throw new Error('插件导航列表无效');
        navigation = source.navigation.map((raw) => {
            const entry = asObject(raw, '插件导航');
            const pluginId = text(entry.plugin_id, '插件导航来源');
            const registered = plugins.find((plugin) => plugin.id === pluginId);
            if (!registered) throw new Error('插件导航来源未注册');
            const page = parsePluginPage(entry, pluginId);
            if (!registered.ui_pages.some((item) => item.id === page.id && item.path === page.path)) {
                throw new Error('插件导航未由元数据声明');
            }
            return { ...page, plugin_id: pluginId };
        });
    }
    if (new Set(navigation.map((page) => page.path)).size !== navigation.length) {
        throw new Error('插件导航路径冲突');
    }
    return { api_version: '1.0', host_running: flag(source.host_running, '宿主状态'),
        plugins, sessions: source.sessions.map(parsePluginSession), navigation };
}

export function parsePluginChallenge(value: unknown): PluginChallenge | null {
    const envelope = asObject(value, '插件交互');
    if (envelope.challenge == null) return null;
    const source = asObject(envelope.challenge, '插件交互');
    if (!['qr', 'notice', 'text', 'input'].includes(String(source.kind))) throw new Error('插件交互类型无效');
    const payload = asObject(source.payload, '交互数据');
    let image: string | null = null;
    if (payload.image_base64 !== undefined) {
        const encoded = text(payload.image_base64, '二维码');
        if (payload.mime !== 'image/png' || encoded.length > 3 * 1024 * 1024
            || encoded.length % 4 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw new Error('插件二维码格式无效');
        let decoded: string;
        try { decoded = atob(encoded); } catch { throw new Error('插件二维码格式无效'); }
        if (decoded.slice(0, 8) !== '\x89PNG\r\n\x1a\n' || decoded.length > 2 * 1024 * 1024) {
            throw new Error('插件二维码格式无效');
        }
        image = ['data:', 'image/png', ';base64,', encoded].join('');
    }
    return { challenge_id: text(source.challenge_id, '交互标识'), kind: source.kind as PluginChallenge['kind'],
        title: text(source.title, '交互标题'),
        expires_at: source.expires_at == null ? null : finite(source.expires_at, '交互有效期'),
        message: payload.message == null ? '' : text(payload.message, '交互说明'), image };
}

export function configFields(schema: Record<string, unknown>): ConfigField[] {
    if (schema.type !== 'object') throw new Error('插件配置契约须为对象');
    const properties = asObject(schema.properties ?? {}, '配置字段');
    const required = strings(schema.required ?? [], '必填配置字段');
    return Object.entries(properties).map(([key, raw]) => {
        const field = asObject(raw, '配置字段');
        if (!['string', 'integer', 'number', 'boolean'].includes(String(field.type))) {
            throw new Error(`配置字段 ${key} 暂无可用的管理控件`);
        }
        const type = field.type as ConfigField['type'];
        const secret = field.writeOnly === true || field.format === 'password';
        const choices = field.enum === undefined ? [] : field.enum;
        if (!Array.isArray(choices) || choices.some((item) => !['string', 'number', 'boolean'].includes(typeof item))) {
            throw new Error(`配置字段 ${key} 的选项无效`);
        }
        return { key, title: typeof field.title === 'string' ? field.title : key,
            description: typeof field.description === 'string' ? field.description : '',
            type, required: required.includes(key), secret, choices,
            defaultValue: type === 'boolean' ? field.default === true
                : secret || field.default == null ? '' : String(field.default),
            ...(typeof field.minimum === 'number' ? { minimum: field.minimum } : {}),
            ...(typeof field.maximum === 'number' ? { maximum: field.maximum } : {}) };
    });
}

export function configPayload(fields: ConfigField[], values: Record<string, string | boolean>) {
    const output: Record<string, unknown> = {};
    for (const field of fields) {
        const value = values[field.key] ?? field.defaultValue;
        if (field.type === 'boolean') {
            if (typeof value !== 'boolean') throw new Error(`${field.title}须为开关值`);
            output[field.key] = value;
            continue;
        }
        if (typeof value !== 'string') throw new Error(`${field.title}格式无效`);
        if (!value) {
            if (field.required) throw new Error(`请填写${field.title}`);
            continue;
        }
        let result: string | number = value;
        if (field.type === 'integer' || field.type === 'number') {
            result = Number(value);
            if (!value.trim() || !Number.isFinite(result)
                || (field.type === 'integer' && !Number.isSafeInteger(result))
                || (field.minimum !== undefined && result < field.minimum)
                || (field.maximum !== undefined && result > field.maximum)) throw new Error(`${field.title}数值无效`);
        }
        if (field.choices.length && !field.choices.includes(result)) throw new Error(`${field.title}选项无效`);
        output[field.key] = result;
    }
    return output;
}

export const PLUGIN_LABELS: Record<PluginState, string> = {
    discovered: '未启动', handshaking: '握手中', starting: '启动中',
    running: '运行中', stopping: '停止中', stopped: '已停止', failed: '异常',
};
export const SESSION_LABELS: Record<PluginSession['state'], string> = {
    starting: '准备登录', login_required: '等待登录', authenticating: '登录中', online: '在线',
    offline: '离线', failed: '登录异常', stopped: '已停止',
};

export function parseAccountSelection(value: unknown): AccountSelection {
    const source = asObject(value, 'OneBot 账号选择');
    if (!Array.isArray(source.accounts)) throw new Error('OneBot 账号列表无效');
    const account = source.account == null ? null : text(source.account, '当前 OneBot 账号');
    if (account !== null && !/^[a-z][a-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(account)) {
        throw new Error('当前 OneBot 账号格式无效');
    }
    const accounts = source.accounts.map((raw) => {
        const entry = asObject(raw, 'OneBot 账号');
        return { platform: text(entry.platform, '平台'), self_id: text(entry.self_id, '账号标识') };
    });
    return { account, accounts, state: text(source.state, 'OneBot 选择状态') };
}
