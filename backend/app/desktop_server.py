from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket

import uvicorn

LISTENING_PREFIX = "BREAKTWENTY_EMBEDDED_BACKEND_LISTENING "
BIND_ERROR_PREFIX = "BREAKTWENTY_EMBEDDED_BACKEND_BIND_ERROR "
LOOPBACK_HOSTS = {"127.0.0.1": socket.AF_INET, "::1": socket.AF_INET6}


def bind_loopback_socket(host: str, port: int) -> socket.socket:
    family = LOOPBACK_HOSTS.get(host)
    if family is None:
        raise ValueError("Embedded backend host must be an exact loopback address")
    if not isinstance(port, int) or port < 0 or port > 65535:
        raise ValueError("Embedded backend port is invalid")
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        return listener
    except Exception:
        listener.close()
        raise


async def serve(host: str, port: int) -> None:
    listener = bind_loopback_socket(host, port)
    actual_port = int(listener.getsockname()[1])
    formatted_host = f"[{host}]" if ":" in host else host
    os.environ["BREAKTWENTY_EMBEDDED_BACKEND_OWNERSHIP_ENDPOINT"] = (
        f"http://{formatted_host}:{actual_port}"
    )
    print(
        f"{LISTENING_PREFIX}{json.dumps({'host': host, 'port': actual_port}, separators=(',', ':'))}",
        flush=True,
    )
    config = uvicorn.Config(
        "app.main:app",
        host=host,
        port=actual_port,
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        await server.serve(sockets=[listener])
    finally:
        listener.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=sorted(LOOPBACK_HOSTS), required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.host, args.port))
    except OSError as exc:
        print(
            f"{BIND_ERROR_PREFIX}{json.dumps({'code': int(exc.errno or 0)}, separators=(',', ':'))}",
            flush=True,
        )
        raise SystemExit(98) from None


if __name__ == "__main__":
    main()
