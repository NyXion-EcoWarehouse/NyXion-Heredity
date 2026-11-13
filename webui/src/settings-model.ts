import { asObject } from './api.ts';
import { INTERFACE_TYPES, type InterfaceState, type InterfaceType } from './network-interfaces-model.ts';

export interface ServicePort {
    interface_id: string; name: string; type: InterfaceType; state: InterfaceState;
    port: number; actual_port: number | null; endpoint: string | null; error: string | null;
}
export interface HostSettings {
    startup: {
        ui_host: string; ui_port: number; configured_ui_port: number;
        env_file: string; restart_required: boolean;
    };
    services: ServicePort[];
    ports_policy: 'per-interface';
}

function text(value: unknown, name: string) {
    if (typeof value !== 'string' || value.length > 8192 || /[\u0000-\u001f\u007f]/.test(value)) {
        throw new Error(`${name}格式无效`);
    }
    return value;
}
function port(value: unknown, name: string) {
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 0 || value > 65535) {
        throw new Error(`${name}须为 0–65535 的整数`);
    }
    return value;
}
function endpoint(value: unknown) {
    if (value === null) return null;
    const result = text(value, '服务端点');
    let parsed: URL;
    try { parsed = new URL(result); } catch { throw new Error('服务端点无效'); }
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(parsed.protocol)
        || !parsed.hostname || parsed.username || parsed.password || parsed.hash
        || result.includes('\\')) throw new Error('服务端点无效');
    return result;
}
export function parseHostSettings(value: unknown): HostSettings {
    const source = asObject(value, '宿主配置'), startup = asObject(source.startup, '启动配置');
    if (!Array.isArray(source.services) || source.ports_policy !== 'per-interface'
        || typeof startup.restart_required !== 'boolean') throw new Error('宿主配置格式无效');
    const services = source.services.map((raw) => {
        const item = asObject(raw, '注册服务');
        const id = text(item.interface_id, '服务接口 ID');
        if (!/^[0-9a-f]{16}$/.test(id) || !INTERFACE_TYPES.includes(item.type as InterfaceType)
            || !['stopped', 'starting', 'running', 'error'].includes(String(item.state))) {
            throw new Error('注册服务身份或状态无效');
        }
        return {
            interface_id: id, name: text(item.name, '服务名称'), type: item.type as InterfaceType,
            state: item.state as InterfaceState, port: port(item.port, '服务配置端口'),
            actual_port: item.actual_port === null ? null : port(item.actual_port, '服务运行端口'),
            endpoint: endpoint(item.endpoint), error: item.error === null ? null : text(item.error, '服务错误'),
        };
    });
    if (new Set(services.map((item) => item.interface_id)).size !== services.length) throw new Error('注册服务 ID 重复');
    return {
        startup: {
            ui_host: text(startup.ui_host, 'WebUI 监听地址'), ui_port: port(startup.ui_port, 'WebUI 当前端口'),
            configured_ui_port: port(startup.configured_ui_port, 'WebUI 配置端口'),
            env_file: text(startup.env_file, '配置文件'), restart_required: startup.restart_required,
        },
        services, ports_policy: 'per-interface',
    };
}
export function hostSettingsPayload(value: string) {
    if (!/^[0-9]{1,5}$/.test(value)) throw new Error('WebUI 端口须为 1–65535 的整数');
    const ui_port = Number(value);
    if (ui_port < 1 || ui_port > 65535) throw new Error('WebUI 端口须为 1–65535 的整数');
    return { ui_port };
}
