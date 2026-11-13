import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react';
import { AlertCircle, CheckCircle2, Circle, Loader2 } from 'lucide-react';

export function Button({ variant = 'primary', size = 'normal', className = '', children, ...props }:
    ButtonHTMLAttributes<HTMLButtonElement> & {
        variant?: 'primary' | 'outline' | 'ghost' | 'danger';
        size?: 'normal' | 'sm' | 'icon';
    }) {
    return <button {...props} className={`button button-${variant} button-${size} ${className}`}>
        {children}
    </button>;
}

export function Badge({ variant = 'secondary', className = '', children, ...props }:
    HTMLAttributes<HTMLSpanElement> & { variant?: 'secondary' | 'outline' | 'success' | 'warning' | 'destructive' }) {
    return <span {...props} className={`badge badge-${variant} ${className}`}>{children}</span>;
}

export function Status({ value, unknown = '未确认', good = '已连接', bad = '未连接' }:
    { value: boolean | null | undefined; unknown?: string; good?: string; bad?: string }) {
    return <span className={`status status-${value === true ? 'good' : value === false ? 'bad' : 'unknown'}`}>
        <Circle size={7} fill="currentColor" />{value === true ? good : value === false ? bad : unknown}
    </span>;
}

export function Notice({ error, children }: { error?: boolean; children: ReactNode }) {
    const Icon = error ? AlertCircle : CheckCircle2;
    return <div className={`notice ${error ? 'notice-error' : ''}`} role={error ? 'alert' : 'status'}>
        <Icon size={16} /><div>{children}</div>
    </div>;
}

export function Loading({ text = '正在读取' }: { text?: string }) {
    return <div className="loading-state" role="status"><Loader2 size={17} className="spin" />{text}</div>;
}

export function PageHeading({ title, subtitle, actions }: {
    title: string; subtitle?: string; actions?: ReactNode;
}) {
    return <div className="page-heading">
        <div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>
        {actions && <div className="page-actions">{actions}</div>}
    </div>;
}

export function Empty({ text }: { text: string }) {
    return <div className="empty-state">{text}</div>;
}
