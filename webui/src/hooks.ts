import { useCallback, useEffect, useRef, useState } from 'react';
import { api, message } from './api';

export function useResource<T>(path: string, parse: (value: unknown) => T) {
    const [value, setValue] = useState<T | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const parseRef = useRef(parse);
    parseRef.current = parse;
    const controller = useRef<AbortController | null>(null);
    const refresh = useCallback(async () => {
        controller.current?.abort();
        const current = new AbortController();
        controller.current = current;
        setLoading(true);
        setError(null);
        try {
            const result = parseRef.current(await api(path, { signal: current.signal }));
            if (!current.signal.aborted) setValue(result);
        } catch (failure) {
            if (!current.signal.aborted) setError(message(failure));
        } finally {
            if (controller.current === current) setLoading(false);
        }
    }, [path]);
    useEffect(() => {
        void refresh();
        return () => controller.current?.abort();
    }, [refresh]);
    return { value, error, loading, refresh, setValue, setError };
}
