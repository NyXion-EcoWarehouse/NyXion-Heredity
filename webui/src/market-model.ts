import { asObject } from './api.ts';

export type MarketStatus = 'active' | 'warning' | 'banned';
export type MarketJobState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
export interface MarketFailure { code: string; message: string }
export interface MarketPackage {
    id: string; name: string; description: string; version: string;
    author: { name: string; github?: string; url?: string };
    permissions: string[]; download_url: string; hash_sha256: string;
    status: MarketStatus; security_advisory: string | null; min_heredity_version: string;
    installed: boolean; installed_version: string | null; compatible: boolean | null;
}
export interface MarketJob {
    job_id: string; kind: 'refresh' | 'install' | 'module_start' | 'module_stop' | 'module_restart'; plugin_id: string | null;
    state: MarketJobState; created_at: number; updated_at: number;
    result: Record<string, unknown> | null; error: MarketFailure | null;
}
export interface MarketSandbox {
    available: boolean | null; runtime: string | null; isolation: string | null; reason: string | null;
}
export interface MarketModule {
    id: string; name: string; version: string; namespace: string;
    state: 'installed' | 'starting' | 'running' | 'stopped' | 'failed';
    permissions: string[]; sandbox: { runtime: string; isolation: string; isolated: boolean; process_running: boolean };
    error: MarketFailure | null; installed_at: number;
}
export interface MarketModules { modules: MarketModule[]; jobs: MarketJob[] }
export interface MarketCatalog {
    index_url: string; last_attempt_at: number | null; last_success_at: number | null;
    refreshing: boolean; stale: boolean; error: MarketFailure | null;
    plugins: MarketPackage[];
    invalid_entries: { position: number; name: string; error: MarketFailure }[];
    jobs: MarketJob[]; sandbox: MarketSandbox;
}

const ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
const SHA256 = /^[0-9a-f]{64}$/;
const PERMISSION = /^[a-z][a-z0-9]*(?:[._:-][a-z0-9*]+)*$/;

function text(value: unknown, name: string, max = 8192) {
    if (typeof value !== 'string' || value.length > max || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) {
        throw new Error(`${name}格式无效`);
    }
    return value;
}
function flag(value: unknown, name: string) {
    if (typeof value !== 'boolean') throw new Error(`${name}格式无效`);
    return value;
}
function timestamp(value: unknown, name: string) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new Error(`${name}格式无效`);
    return value;
}
function nullableTime(value: unknown, name: string) {
    return value == null ? null : timestamp(value, name);
}
function packageId(value: unknown) {
    const result = text(value, '模块 ID', 128);
    if (!ID.test(result)) throw new Error('模块 ID 格式无效');
    return result;
}
function remoteUrl(value: unknown, name: string) {
    const result = text(value, name, 8192);
    let parsed: URL;
    try { parsed = new URL(result); } catch { throw new Error(`${name}格式无效`); }
    if (!['https:', 'http:'].includes(parsed.protocol) || !parsed.hostname
        || parsed.username || parsed.password || parsed.hash || result.includes('\\')
        || /[\u0000-\u0020\u007f]/.test(result)) throw new Error(`${name}格式无效`);
    return result;
}
function failure(value: unknown): MarketFailure | null {
    if (value == null) return null;
    const source = asObject(value, '商场错误');
    return { code: text(source.code, '错误代码', 128), message: text(source.message, '错误详情') };
}
function permissions(value: unknown) {
    if (!Array.isArray(value) || value.length > 128) throw new Error('模块权限列表无效');
    const output = value.map((entry) => {
        const result = text(entry, '权限', 128);
        if (!PERMISSION.test(result)) throw new Error('模块权限名称无效');
        return result;
    });
    if (new Set(output).size !== output.length) throw new Error('模块权限重复');
    return output;
}

export function parseMarketPackage(value: unknown): MarketPackage {
    const source = asObject(value, '商场模块');
    const author = asObject(source.author, '模块作者');
    const hash = text(source.hash_sha256, '包校验值', 64);
    if (!SHA256.test(hash)) throw new Error('包 SHA-256 无效');
    if (!['active', 'warning', 'banned'].includes(String(source.status))) throw new Error('模块安全状态无效');
    let installed = false, installed_version = null;
    if (typeof source.installed === 'boolean') installed = source.installed;
    else if (source.installed != null) {
        const record = asObject(source.installed, '已安装模块');
        installed = true;
        installed_version = text(record.version, '已安装版本', 80);
    }
    return {
        id: packageId(source.id), name: text(source.name, '模块名称', 160),
        description: text(source.description, '模块说明'), version: text(source.version, '模块版本', 80),
        author: { name: text(author.name, '作者名称', 160),
            ...(author.github == null ? {} : { github: text(author.github, '作者 GitHub', 2048) }),
            ...(author.url == null ? {} : { url: remoteUrl(author.url, '作者地址') }) },
        permissions: permissions(source.permissions),
        download_url: remoteUrl(source.download_url, '模块包地址'),
        hash_sha256: hash, status: source.status as MarketStatus,
        security_advisory: source.security_advisory == null ? null : text(source.security_advisory, '安全提示'),
        min_heredity_version: text(source.min_heredity_version, '最低宿主版本', 80),
        installed, installed_version,
        compatible: source.compatible === undefined ? null : flag(source.compatible, '版本兼容性'),
    };
}

export function parseMarketJob(value: unknown): MarketJob {
    const source = asObject(value, '商场任务');
    const job_id = text(source.job_id, '任务 ID', 128);
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(job_id)
        || !['refresh', 'install', 'module_start', 'module_stop', 'module_restart'].includes(String(source.kind))
        || !['queued', 'running', 'completed', 'failed', 'cancelled'].includes(String(source.state))) {
        throw new Error('商场任务状态无效');
    }
    return { job_id, kind: source.kind as MarketJob['kind'],
        plugin_id: source.plugin_id == null ? null : packageId(source.plugin_id),
        state: source.state as MarketJobState, created_at: timestamp(source.created_at, '任务创建时间'),
        updated_at: timestamp(source.updated_at, '任务更新时间'),
        result: source.result == null ? null : asObject(source.result, '任务结果'), error: failure(source.error) };
}

export function parseMarketModules(value: unknown): MarketModules {
    const source = asObject(value, '已安装商场模块');
    if (!Array.isArray(source.modules) || !Array.isArray(source.jobs)) throw new Error('已安装模块列表无效');
    const modules = source.modules.map((raw) => {
        const item = asObject(raw, '已安装模块'), sandbox = asObject(item.sandbox, '模块隔离状态');
        const id = packageId(item.id);
        if (item.namespace !== `/modules/${id}`
            || !['installed', 'starting', 'running', 'stopped', 'failed'].includes(String(item.state))) {
            throw new Error('已安装模块的运行状态无效');
        }
        return {
            id, name: text(item.name, '模块名称', 160), version: text(item.version, '模块版本', 80),
            namespace: item.namespace as string, state: item.state as MarketModule['state'],
            permissions: permissions(item.permissions),
            sandbox: { runtime: text(sandbox.runtime, '模块运行时', 128),
                isolation: text(sandbox.isolation, '隔离方式', 128),
                isolated: flag(sandbox.isolated, '模块隔离状态'),
                process_running: flag(sandbox.process_running, '模块进程状态') },
            error: failure(item.error), installed_at: timestamp(item.installed_at, '模块安装时间'),
        };
    });
    if (new Set(modules.map((item) => item.id)).size !== modules.length) throw new Error('已安装模块 ID 重复');
    return { modules, jobs: source.jobs.map(parseMarketJob) };
}

export function parseMarketCatalog(value: unknown): MarketCatalog {
    const source = asObject(value, '模块商场');
    if (!Array.isArray(source.plugins) || !Array.isArray(source.invalid_entries) || !Array.isArray(source.jobs)) {
        throw new Error('商场目录格式无效');
    }
    const plugins = source.plugins.map(parseMarketPackage);
    if (new Set(plugins.map((entry) => entry.id)).size !== plugins.length) throw new Error('商场模块 ID 重复');
    const sandbox = asObject(source.sandbox, '运行隔离状态');
    const support = sandbox.available ?? sandbox.supported;
    return {
        index_url: remoteUrl(source.index_url, '目录来源'),
        last_attempt_at: nullableTime(source.last_attempt_at, '最近刷新'),
        last_success_at: nullableTime(source.last_success_at, '最近成功刷新'),
        refreshing: flag(source.refreshing, '目录刷新状态'),
        stale: source.stale === undefined ? false : flag(source.stale, '缓存状态'),
        error: failure(source.error), plugins,
        invalid_entries: source.invalid_entries.map((raw) => {
            const entry = asObject(raw, '不可用商场条目');
            const position = timestamp(entry.position, '条目位置'), error = failure(entry.error);
            if (!Number.isInteger(position) || !error) throw new Error('无效目录条目错误信息');
            return { position, name: text(entry.name, '条目名称', 160), error };
        }),
        jobs: source.jobs.map(parseMarketJob),
        sandbox: {
            available: support === undefined ? null : flag(support, '运行隔离能力'),
            runtime: sandbox.runtime == null ? null : text(sandbox.runtime, '模块运行时', 128),
            isolation: sandbox.isolation == null ? null : text(sandbox.isolation, '隔离机制', 128),
            reason: sandbox.reason == null ? null : text(sandbox.reason, '运行限制'),
        },
    };
}

const PERMISSION_LABELS: Record<string, [string, string]> = {
    'net.connect': ['网络连接', '连接外部网络服务'],
    'net.listen': ['网络监听', '在本机接受网络连接'],
    'fs.read': ['读取文件', '读取获准目录内的文件'],
    'fs.write': ['写入文件', '创建或修改获准目录内的文件'],
    'env.read': ['读取环境', '读取运行环境中的配置'],
    'process.spawn': ['启动进程', '创建本机子进程'],
    'process.exec': ['执行程序', '执行本机程序'],
    'native.load': ['加载本机代码', '加载本机原生组件'],
    'bus.call': ['调用宿主动作', '通过宿主总线调用动作'],
    'bus.subscribe': ['订阅宿主消息', '通过宿主总线接收消息'],
    'bus.publish': ['发布宿主消息', '向宿主总线发布消息'],
    'network.call': ['调用网络接口', '使用分配给模块的网络接口'],
    'network.receive': ['接收网络消息', '接收已分配接口上的消息'],
    'network.publish': ['发布网络事件', '向已分配接口发布事件'],
};
export function permissionDescription(value: string) {
    const [label, description] = PERMISSION_LABELS[value] ?? ['其他权限', '此权限的范围由宿主运行时约束'];
    return { label, description, id: value };
}

export function compatibleVersion(current: string, minimum: string): boolean {
    const parse = (value: string, host = false) => {
        const post = host ? /^(\d+)\.(\d+)\.(\d+)\.post(0|[1-9]\d*)$/.exec(value) : null;
        const semver = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(value);
        const match = post ?? semver;
        if (!match) return null;
        const release = match.slice(1, 4).map(Number);
        const revision = post ? Number(post[4]) : 0;
        if (release.some((part) => !Number.isSafeInteger(part)) || !Number.isSafeInteger(revision)) return null;
        return { release, prerelease: post ? '' : semver![4] ?? '', revision };
    };
    const first = parse(current, true), second = parse(minimum);
    if (!first || !second) return false;
    for (let position = 0; position < 3; position += 1) {
        const difference = first.release[position] - second.release[position];
        if (difference) return difference > 0;
    }
    if (!first.prerelease && !second.prerelease) return first.revision >= second.revision;
    if (!first.prerelease) return true;
    if (!second.prerelease) return false;
    const a = first.prerelease.split('.'), b = second.prerelease.split('.');
    for (let position = 0; position < Math.max(a.length, b.length); position += 1) {
        if (a[position] === undefined) return false;
        if (b[position] === undefined) return true;
        if (a[position] === b[position]) continue;
        const numericA = /^\d+$/.test(a[position]), numericB = /^\d+$/.test(b[position]);
        if (numericA && numericB) return Number(a[position]) > Number(b[position]);
        if (numericA !== numericB) return !numericA;
        return a[position] > b[position];
    }
    return true;
}
export function packageCompatible(item: MarketPackage, hostVersion: string) {
    return item.compatible ?? compatibleVersion(hostVersion, item.min_heredity_version);
}
export function marketInstallPayload(item: MarketPackage, hostVersion: string,
    confirmed: string[], forceWarning: boolean) {
    if (item.status === 'banned') throw new Error('此模块已封禁，无法安装');
    if (!packageCompatible(item, hostVersion)) throw new Error('当前宿主版本不满足模块要求');
    if (item.status === 'warning' && !forceWarning) throw new Error('请先确认模块安全提示');
    if (confirmed.length !== item.permissions.length || new Set(confirmed).size !== confirmed.length
        || !item.permissions.every((permission) => confirmed.includes(permission))) {
        throw new Error('请逐项确认模块所需权限');
    }
    return { plugin_id: item.id, expected_version: item.version, expected_sha256: item.hash_sha256,
        confirmed_permissions: [...item.permissions], force_warning: item.status === 'warning' && forceWarning };
}
export function marketJobPath(id: string) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(id)) throw new Error('任务 ID 无效');
    return `/api/heredity/market/jobs/${encodeURIComponent(id)}`;
}
export function marketModulePath(id: string, command: 'start' | 'stop' | 'restart') {
    packageId(id);
    if (!['start', 'stop', 'restart'].includes(command)) throw new Error('模块操作无效');
    return `/api/heredity/market/modules/${encodeURIComponent(id)}/${command}`;
}
export function isMarketJobActive(job: MarketJob) {
    return job.state === 'queued' || job.state === 'running';
}
export function packageSourceLabel(url: string) {
    const parsed = new URL(url);
    return parsed.host + parsed.pathname;
}
export const MARKET_JOB_LABELS: Record<MarketJobState, string> = {
    queued: '等待处理', running: '处理中', completed: '已完成', failed: '失败', cancelled: '已取消',
};
