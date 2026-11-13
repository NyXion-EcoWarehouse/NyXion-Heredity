"""Stable listener failures without exposing native exception text or secrets."""

import errno
import socket


def listener_failure(error, port, *, host=None):
    if getattr(error, "winerror", None) == 10013 and host is not None and port:
        # Windows HTTP reuse can turn an exclusive listener collision into
        # WSAEACCES. A non-reusing bind distinguishes it from a denied port.
        probe = None
        try:
            probe = socket.socket(
                socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
            probe.bind((host, port))
        except OSError as bind_error:
            if getattr(bind_error, "winerror", None) == 10048:
                return "PORT_IN_USE", f"端口 {port} 已被占用，请修改端口或关闭占用程序后重试。"
        finally:
            if probe is not None:
                probe.close()
    if (getattr(error, "code", None) == "PORT_IN_USE"
            or getattr(error, "errno", None) in {errno.EADDRINUSE, 48, 98, 10048}
            or getattr(error, "winerror", None) == 10048):
        return "PORT_IN_USE", f"端口 {port} 已被占用，请修改端口或关闭占用程序后重试。"
    return "NETWORK_BIND_FAILED", f"无法监听端口 {port}，请检查监听地址和系统权限后重试。"
