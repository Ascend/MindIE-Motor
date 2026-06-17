#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Small stdlib TCP forwarder for Docker-to-PodIP bridge targets."""

from __future__ import annotations

import argparse
import selectors
import socket
import sys
import threading
from typing import Tuple


def _pipe(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            for key, _ in selector.select():
                src = key.fileobj
                dst = key.data
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    finally:
        selector.close()
        left.close()
        right.close()


def _handle(client: socket.socket, addr: Tuple[str, int], target_host: str, target_port: int) -> None:
    del addr
    try:
        upstream = socket.create_connection((target_host, target_port), timeout=5)
    except OSError:
        client.close()
        return
    _pipe(client, upstream)


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward one local TCP port to one remote target.")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.listen_host, args.listen_port))
    server.listen(128)
    print(
        f"[tcp-forward] {args.listen_host}:{args.listen_port} -> {args.target_host}:{args.target_port}",
        flush=True,
    )
    try:
        while True:
            client, addr = server.accept()
            thread = threading.Thread(
                target=_handle,
                args=(client, addr, args.target_host, args.target_port),
                daemon=True,
            )
            thread.start()
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    sys.exit(main())
