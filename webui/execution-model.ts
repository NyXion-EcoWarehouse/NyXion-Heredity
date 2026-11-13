export const EXECUTION_LEVELS = ['L0', 'L1', 'L2', 'L3'] as const;
export type ExecutionLevel = (typeof EXECUTION_LEVELS)[number];
export type Availability = 'ready' | 'partial' | 'unimplemented' | 'configuration_required';
export type Provenance = 'none' | 'declared' | 'implementation';
export type SourceKind = 'demo' | 'profile' | 'native' | 'unconfigured';

export interface ExecutionLevelDefinition {
    level: ExecutionLevel;
    label: string;
    description: string;
}

export interface ProviderDescriptor {
    provider_id: string;
    label: string;
    execution_level: ExecutionLevel | null;
    source_kind: SourceKind;
    host_kind: string;
    session_owner: string;
    target_memory_access: string;
    in_process_code: boolean;
    business_request_builder: string;
    primary_transport_owner: string;
    auxiliary_transports: string[];
    event_sources: string[];
    supported_actions: string[];
    receipt_evidence: string[];
    availability: Availability;
    reason: string;
    level_provenance: Provenance;
    selectable: boolean;
}

export interface ExecutionState {
    state: string;
    active_provider_id: string | null;
    session_epoch: string;
    descriptor: ProviderDescriptor | null;
    providers: ProviderDescriptor[];
    in_flight: number;
}

export interface ExecutionPayload {
    levels: ExecutionLevelDefinition[];
    state: ExecutionState;
    canSwitch: boolean;
}

export class ExecutionApiError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
        super(message);
        this.name = 'ExecutionApiError';
        this.status = status;
    }
}

function object(value: unknown, name: string): Record<string, unknown> {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error(`${name} 格式无效`);
    }
    return value as Record<string, unknown>;
}

function string(value: unknown, name: string, allowEmpty = false): string {
    if (typeof value !== 'string' || (!allowEmpty && !value)) {
        throw new Error(`${name} 格式无效`);
    }
    return value;
}

function boolean(value: unknown, name: string): boolean {
    if (typeof value !== 'boolean') throw new Error(`${name} 格式无效`);
    return value;
}

function strings(value: unknown, name: string): string[] {
    if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
        throw new Error(`${name} 格式无效`);
    }
    return [...value] as string[];
}

function member<T extends string>(value: unknown, choices: readonly T[], name: string): T {
    if (typeof value !== 'string' || !choices.includes(value as T)) throw new Error(`${name} 格式无效`);
    return value as T;
}

export function parseProvider(value: unknown): ProviderDescriptor {
    const source = object(value, '提供方');
    return {
        provider_id: string(source.provider_id, '提供方 ID'), label: string(source.label, '提供方名称'),
        execution_level: source.execution_level === null
            ? null : member(source.execution_level, EXECUTION_LEVELS, '执行级别'),
        source_kind: member(source.source_kind, ['demo', 'profile', 'native', 'unconfigured'], '来源类型'),
        host_kind: string(source.host_kind, '宿主类型'),
        session_owner: string(source.session_owner, '会话所有者'),
        target_memory_access: string(source.target_memory_access, '目标内存访问'),
        in_process_code: boolean(source.in_process_code, '进程内代码'),
        business_request_builder: string(source.business_request_builder, '业务请求构造者'),
        primary_transport_owner: string(source.primary_transport_owner, '主传输所有者'),
        auxiliary_transports: strings(source.auxiliary_transports, '辅助传输'),
        event_sources: strings(source.event_sources, '事件来源'),
        supported_actions: strings(source.supported_actions, '支持动作'),
        receipt_evidence: strings(source.receipt_evidence, '回执证据'),
        availability: member(source.availability,
            ['ready', 'partial', 'unimplemented', 'configuration_required'], '可用状态'),
        reason: string(source.reason, '状态说明', true),
        level_provenance: member(source.level_provenance, ['none', 'declared', 'implementation'], '级别依据'),
        selectable: boolean(source.selectable, '可切换状态'),
    };
}

export function parseExecutionState(value: unknown): ExecutionState {
    const source = object(value, '执行状态');
    if (typeof source.in_flight !== 'number' || !Number.isSafeInteger(source.in_flight)
        || source.in_flight < 0) throw new Error('在途操作数量格式无效');
    if (!Array.isArray(source.providers)) throw new Error('提供方列表格式无效');
    const providers = source.providers.map(parseProvider);
    if (new Set(providers.map((provider) => provider.provider_id)).size !== providers.length) {
        throw new Error('提供方 ID 重复');
    }
    const descriptor = source.descriptor === null ? null : parseProvider(source.descriptor);
    const activeId = source.active_provider_id === null
        ? null : string(source.active_provider_id, '活动提供方');
    if ((descriptor === null) !== (activeId === null)
        || (descriptor && descriptor.provider_id !== activeId)) {
        throw new Error('活动提供方与描述不一致');
    }
    return {
        state: string(source.state, '运行状态'), active_provider_id: activeId,
        session_epoch: string(source.session_epoch, '会话代次'), descriptor, providers,
        in_flight: source.in_flight,
    };
}

export function parseExecutionPayload(value: unknown): ExecutionPayload {
    const source = object(value, '执行级别响应');
    if (!Array.isArray(source.levels)) throw new Error('执行级别列表格式无效');
    const levels = source.levels.map((item) => {
        const level = object(item, '执行级别');
        return {
            level: member(level.level, EXECUTION_LEVELS, '执行级别'),
            label: string(level.label, '级别名称'), description: string(level.description, '级别说明'),
        };
    });
    if (levels.length !== 4 || new Set(levels.map((level) => level.level)).size !== 4) {
        throw new Error('执行级别必须完整包含 L0、L1、L2、L3');
    }
    return {
        levels: EXECUTION_LEVELS.map((id) => levels.find((level) => level.level === id)!),
        state: parseExecutionState(source.state), canSwitch: boolean(source.canSwitch, '管理通道状态'),
    };
}

export function parseSwitchResult(value: unknown): { state: ExecutionState; changed: boolean } {
    const source = object(value, '切换响应');
    return { state: parseExecutionState(source.state), changed: boolean(source.changed, '切换结果') };
}

export function availabilityLabel(value: Availability): string {
    return { ready: '可连接', partial: '部分实现', unimplemented: '未实现',
        configuration_required: '待配置' }[value];
}

export function provenanceLabel(value: Provenance): string {
    return { none: '未分配级别', declared: '配置声明 · 未验证', implementation: '实现确认' }[value];
}

export function sourceLabel(value: SourceKind): string {
    return { demo: 'DEMO', profile: '配置接口', native: '原生提供方', unconfigured: '未配置' }[value];
}

export function managerStateLabel(value: string): string {
    return {
        unconfigured: '未配置', initializing: '初始化中', connected: '已连接', ready: '就绪', draining: '排空中',
        disconnected: '未连接', close_failed: '关闭失败', switching: '切换中', failed: '失败',
        closing: '关闭中', closed: '已关闭',
    }[value] ?? value;
}

export function shortEpoch(value: string): string {
    return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

export function canSelectProvider(
    provider: ProviderDescriptor, state: ExecutionState, canSwitch: boolean, busy: boolean,
): boolean {
    return canSwitch && !busy && provider.selectable && provider.availability === 'ready'
        && provider.provider_id !== state.active_provider_id
        && !['initializing', 'draining', 'switching', 'closing', 'closed'].includes(state.state);
}
