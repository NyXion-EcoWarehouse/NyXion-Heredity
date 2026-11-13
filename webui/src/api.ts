const TOKEN_KEY = 'heredity_token';

export class ApiError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
    }
}

export function getToken(): string | null {
    try {
        return localStorage.getItem(TOKEN_KEY);
    } catch {
        return null;
    }
}

export function saveToken(token: string | null): void {
    try {
        if (token) localStorage.setItem(TOKEN_KEY, token);
        else localStorage.removeItem(TOKEN_KEY);
    } catch {
        throw new Error('浏览器无法保存登录状态');
    }
}

export async function api(path: string, options: RequestInit = {}): Promise<unknown> {
    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort('timeout'), 20000);
    const signal = options.signal ? AbortSignal.any([options.signal, deadline.signal]) : deadline.signal;
    const headers = new Headers(options.headers);
    const token = getToken();
    if (token) headers.set('X-Heredity-Token', token);
    if (options.body) headers.set('Content-Type', 'application/json');
    try {
        const response = await fetch(path, { ...options, headers, signal });
        let payload: unknown;
        try {
            payload = await response.json();
        } catch {
            throw new ApiError(response.status || 502, '服务返回了无效的 JSON 响应');
        }
        if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
            throw new ApiError(502, '服务返回了无效的数据格式');
        }
        if (!response.ok) {
            const body = payload as Record<string, unknown>;
            if (response.status === 401 && path !== '/api/login') {
                window.dispatchEvent(new Event('heredity:session-expired'));
            }
            throw new ApiError(response.status,
                typeof body.message === 'string' ? body.message
                    : typeof body.error === 'string' ? body.error : `请求失败（${response.status}）`);
        }
        return payload;
    } catch (error) {
        if (deadline.signal.aborted) throw new ApiError(408, '请求超时，结果尚未确认');
        throw error;
    } finally {
        clearTimeout(timer);
    }
}

export function post(path: string, payload: unknown, signal?: AbortSignal) {
    return api(path, { method: 'POST', body: JSON.stringify(payload), signal });
}

export function asObject(value: unknown, name = '响应'): Record<string, unknown> {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error(`${name}格式无效`);
    }
    return value as Record<string, unknown>;
}

export function message(error: unknown): string {
    return error instanceof Error ? error.message : '操作失败';
}
