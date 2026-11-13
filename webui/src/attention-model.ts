import { asObject } from './api.ts';

export interface AttentionItem {
    path: string;
    severity: 'error' | 'warning'; count: number; message: string;
}
export interface HostAttention { items: AttentionItem[]; healthy: boolean }

const PATHS = ['/connections', '/market', '/plugins', '/settings'] as const;
const PLUGIN_PAGE = /^\/plugins\/[a-z][a-z0-9_.-]{0,127}\/[a-z][a-z0-9-]{0,63}$/;
export function parseHostAttention(value: unknown): HostAttention {
    const source = asObject(value, '宿主提醒');
    if (!Array.isArray(source.items) || source.items.length > 64 || typeof source.healthy !== 'boolean') {
        throw new Error('宿主提醒格式无效');
    }
    const items = source.items.flatMap((raw) => {
        const item = asObject(raw, '宿主提醒条目');
        if (typeof item.path !== 'string'
            || (!PATHS.includes(item.path as typeof PATHS[number]) && !PLUGIN_PAGE.test(item.path))) return [];
        if (!['error', 'warning'].includes(String(item.severity))
            || typeof item.count !== 'number' || !Number.isSafeInteger(item.count)
            || item.count < 1 || item.count > 1000000
            || typeof item.message !== 'string' || !item.message || item.message.length > 2048
            || /[\u0000-\u001f\u007f]/.test(item.message)) throw new Error('宿主提醒条目无效');
        return [{ path: item.path, severity: item.severity as AttentionItem['severity'],
            count: item.count, message: item.message }];
    });
    return { items, healthy: source.healthy };
}
export function attentionForPath(items: AttentionItem[], path: string) {
    const matches = items.filter((item) => item.path === path);
    if (!matches.length) return null;
    return {
        severity: matches.some((item) => item.severity === 'error') ? 'error' as const : 'warning' as const,
        count: matches.reduce((total, item) => total + item.count, 0),
        message: matches.map((item) => item.message).join('；'),
    };
}
