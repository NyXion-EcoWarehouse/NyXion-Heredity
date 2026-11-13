import { asObject } from './api.ts';
import { INTERFACE_TYPES, type InterfaceType, type NetworkInterface } from './network-interfaces-model.ts';

export type NetworkDirection = 'call' | 'publish' | 'receive';
export type RequestState = 'pending' | 'granted' | 'denied' | 'released' | 'revoked' | 'expired';
export interface NetworkRequirement {
    module_id: string;
    transport_types: InterfaceType[];
    directions: NetworkDirection[];
    account: string | null;
    purpose: string;
    max_instances: number;
    max_interfaces: number;
}
export interface NetworkGrant {
    request_id: string;
    module_id: string;
    interface_id: string;
    generation: number;
    transport_type: InterfaceType;
    account: string | null;
    expires_at: number | null;
    grant_id?: string;
}
export interface NetworkRequest {
    request_id: string;
    module_id: string;
    requirement: NetworkRequirement;
    state: RequestState;
    grant: NetworkGrant | null;
    grants: NetworkGrant[];
    error: string | null;
    created_at: number;
    auto_start: boolean;
    saved_interface_ids: string[];
    can_reapply: boolean;
    module_active: boolean;
    superseded_by: string | null;
    restoring: boolean;
}

const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const MODULE_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
const ACCOUNT = /^[a-z][a-z0-9_.-]{0,127}:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const STATES = ['pending', 'granted', 'denied', 'released', 'revoked', 'expired'] as const;
const DIRECTIONS = ['call', 'publish', 'receive'] as const;

function identifier(value: unknown, pattern: RegExp, name: string) {
    if (typeof value !== 'string' || value.length > 256 || !pattern.test(value)) {
        throw new Error(`${name}无效`);
    }
    return value;
}
function account(value: unknown) {
    return value === null ? null : identifier(value, ACCOUNT, '模块申请账号');
}
function finite(value: unknown, name: string) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new Error(`${name}无效`);
    return value;
}
function transport(value: unknown): InterfaceType {
    if (!INTERFACE_TYPES.includes(value as InterfaceType)) throw new Error('模块申请接口类型无效');
    return value as InterfaceType;
}
function unique<T>(value: unknown, parse: (item: unknown) => T, name: string): T[] {
    if (!Array.isArray(value) || !value.length || value.length > 8) throw new Error(`${name}无效`);
    const output = value.map(parse);
    if (new Set(output).size !== output.length) throw new Error(`${name}包含重复值`);
    return output;
}

function flag(value: unknown, fallback: boolean, label: string) {
    if (value === undefined) return fallback;
    if (typeof value !== 'boolean') throw new Error(`${label}无效`);
    return value;
}

export function parseNetworkRequest(raw: unknown): NetworkRequest {
        const entry = asObject(raw, '模块申请');
        const request_id = identifier(entry.request_id, REQUEST_ID, '模块申请 ID');
        const module_id = identifier(entry.module_id, MODULE_ID, '模块 ID');
        const requirement = asObject(entry.requirement, '模块接口需求');
        if (requirement.module_id !== module_id || typeof requirement.purpose !== 'string'
            || requirement.purpose.length > 256 || /[\u0000-\u001f\u007f]/.test(requirement.purpose)
            || !Number.isInteger(requirement.max_instances) || (requirement.max_instances as number) < 1
            || (requirement.max_instances as number) > 64) throw new Error('模块接口需求无效');
        const checked: NetworkRequirement = {
            module_id,
            transport_types: unique(requirement.transport_types, transport, '模块申请接口类型'),
            directions: unique(requirement.directions, (direction) => {
                if (!DIRECTIONS.includes(direction as NetworkDirection)) throw new Error('模块接口方向无效');
                return direction as NetworkDirection;
            }, '模块申请接口方向'),
            account: account(requirement.account), purpose: requirement.purpose,
            max_instances: requirement.max_instances as number,
            max_interfaces: requirement.max_interfaces === undefined ? 16
                : finite(requirement.max_interfaces, '模块接口数量上限'),
        };
        if (!Number.isInteger(checked.max_interfaces) || checked.max_interfaces < 1
            || checked.max_interfaces > 64) throw new Error('模块接口数量上限无效');
        if (!STATES.includes(entry.state as RequestState)) throw new Error('模块申请状态无效');
        const parseGrant = (rawGrant: unknown): NetworkGrant => {
            const allocation = asObject(rawGrant, '模块接口分配');
            if (allocation.request_id !== request_id || allocation.module_id !== module_id
                || !Number.isSafeInteger(allocation.generation) || (allocation.generation as number) < 1) {
                throw new Error('模块接口分配与申请不一致');
            }
            const interface_id = identifier(allocation.interface_id, /^[0-9a-f]{16}$/, '已分配接口 ID');
            const transport_type = transport(allocation.transport_type);
            const selectedAccount = account(allocation.account);
            if (!checked.transport_types.includes(transport_type)
                || (checked.account !== null && selectedAccount !== checked.account)) {
                throw new Error('已分配接口不满足模块需求');
            }
            return {
                request_id, module_id, interface_id,
                generation: allocation.generation as number, transport_type,
                account: selectedAccount,
                expires_at: allocation.expires_at === null ? null
                    : finite(allocation.expires_at, '接口分配有效期'),
                ...(allocation.grant_id == null ? {}
                    : { grant_id: identifier(allocation.grant_id, REQUEST_ID, '接口授权 ID') }),
            };
        };
        const grant = entry.grant == null ? null : parseGrant(entry.grant);
        if (entry.grants !== undefined && !Array.isArray(entry.grants)) throw new Error('模块接口分配列表无效');
        const grants = entry.grants === undefined ? (entry.state === 'granted' && grant ? [grant] : [])
            : (entry.grants as unknown[]).map(parseGrant);
        if (grants.length > checked.max_interfaces
            || new Set(grants.map((item) => item.interface_id)).size !== grants.length) {
            throw new Error('模块接口分配数量超限或 ID 重复');
        }
        if (entry.state === 'granted' && !grants.length) throw new Error('模块申请缺少已分配接口');
        if (entry.grants !== undefined && (grant?.interface_id ?? null) !== (grants[0]?.interface_id ?? null)) {
            throw new Error('模块接口分配首项不一致');
        }
        if (entry.error !== undefined && entry.error !== null && typeof entry.error !== 'string') {
            throw new Error('模块接口申请错误格式无效');
        }
        const saved = entry.saved_interface_ids ?? [];
        if (!Array.isArray(saved) || saved.length > checked.max_interfaces
            || new Set(saved).size !== saved.length) throw new Error('已保存接口列表无效');
        return {
            request_id, module_id, requirement: checked, state: entry.state as RequestState,
            grant, grants, error: entry.error == null ? null : entry.error as string,
            created_at: finite(entry.created_at, '模块申请时间'),
            auto_start: flag(entry.auto_start, true, '自动启动策略'),
            saved_interface_ids: saved.map((id) => identifier(id, /^[0-9a-f]{16}$/, '已保存接口 ID')),
            can_reapply: flag(entry.can_reapply, false, '重新申请状态'),
            module_active: flag(entry.module_active, true, '模块运行状态'),
            superseded_by: entry.superseded_by == null ? null
                : identifier(entry.superseded_by, REQUEST_ID, '后继申请 ID'),
            restoring: flag(entry.restoring, false, '接口恢复状态'),
        };
}

export function parseNetworkRequests(value: unknown): NetworkRequest[] {
    const source = asObject(value, '模块接口申请');
    if (!Array.isArray(source.requests)) throw new Error('模块接口申请列表无效');
    const requests = source.requests.map(parseNetworkRequest);
    if (new Set(requests.map((entry) => entry.request_id)).size !== requests.length) {
        throw new Error('模块接口申请 ID 重复');
    }
    return requests;
}

export function canAssignRequest(request: NetworkRequest) {
    return request.state !== 'released' && request.module_active !== false
        && request.superseded_by == null;
}

export function currentNetworkRequests(requests: NetworkRequest[]) {
    const candidates = requests.filter((entry) => entry.superseded_by === null);
    return candidates.filter((entry) => {
        if (entry.state !== 'released') return true;
        const matches = candidates.filter((other) => other.module_id === entry.module_id
            && other.requirement.account === entry.requirement.account);
        if (matches.some((other) => other.state !== 'released')) return false;
        return matches.reduce((latest, other) => other.created_at > latest.created_at
            || (other.created_at === latest.created_at && other.request_id > latest.request_id) ? other : latest,
            entry).request_id === entry.request_id;
    });
}

export function historyNetworkRequests(all: NetworkRequest[], current: NetworkRequest[]) {
    const currentIds = new Set(current.map((entry) => entry.request_id));
    return all.filter((entry) => !currentIds.has(entry.request_id))
        .sort((first, second) => second.created_at - first.created_at);
}

export function assignmentPayload(request: NetworkRequest, selected: string[]) {
    if (!canAssignRequest(request)) throw new Error('模块已释放此申请，请等待重新声明');
    if (!Array.isArray(selected) || selected.length > request.requirement.max_interfaces
        || selected.some((id) => typeof id !== 'string' || !/^[0-9a-f]{16}$/.test(id))
        || new Set(selected).size !== selected.length) throw new Error('所选接口无效、重复或数量超限');
    return { interface_ids: [...selected] };
}

export function sameInterfaceSelection(first: string[], second: string[]) {
    return first.length === second.length && first.every((id) => second.includes(id));
}

export function assignableInterfaces(request: NetworkRequest, interfaces: NetworkInterface[]) {
    const requirement = request.requirement;
    return interfaces.filter((item) => requirement.transport_types.includes(item.type)
        && (requirement.account === null || item.account === null || item.account === requirement.account));
}

export function networkRequestPath(id: string) {
    identifier(id, REQUEST_ID, '模块申请 ID');
    return `/api/heredity/network-requests/${encodeURIComponent(id)}`;
}

export const REQUEST_STATE_LABELS: Record<RequestState, string> = {
    pending: '待分配', granted: '已分配', denied: '已拒绝', released: '已释放', revoked: '已撤销', expired: '已过期',
};
