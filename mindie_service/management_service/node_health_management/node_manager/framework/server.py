#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import ipaddress
import os
import socket
import ssl

import uvicorn
from fastapi import FastAPI

from node_manager.common.utils import _SingletonMeta
from node_manager.routes.server_api import router
from node_manager.common.logging import Log
from node_manager.framework.utils import CertUtil
from node_manager.framework.utils.cert_utils import CA_CERTS, TLS_CERT, TLS_KEY

logger = Log(__name__).getlog()

DEFAULT_IP = '127.0.0.1'
DEFAULT_PORT = 3456
IPV4 = 4
IPV6 = 6
MAX_CONNECTED_CLIENT_NUM = 8


class Server(FastAPI, metaclass=_SingletonMeta):
    def __init__(self):
        super().__init__()
        self._server = None
        self.include_router(router)

    @property
    def should_exit(self) -> bool:
        """
        Public view of server shutdown flag.

        This avoids tests (and callers) touching the private ``_server`` member.
        """
        return bool(self._server and getattr(self._server, "should_exit", False))

    def run(self, ms_node_manager: dict = None):
        if not ms_node_manager:
            from node_manager.core.config import GeneralConfig
            ms_node_manager = GeneralConfig().http_server_config

        ip = os.getenv('POD_IP', DEFAULT_IP)
        port = ms_node_manager.get('node_manager_port', DEFAULT_PORT)
        enable_tls_verify = ms_node_manager.get('tls_config', {}).get('server_tls_enable', False)
        http_type = 'https' if enable_tls_verify else 'http'
        password = None
        sock = None
        try:
            if enable_tls_verify:
                tls_items = ms_node_manager.get('tls_config', {}).get('server_tls_items', {})
                password = CertUtil.validate_cert_and_decrypt_password(tls_items)
                config = uvicorn.Config(
                    self,
                    reload=False,
                    ssl_keyfile=tls_items.get(TLS_KEY),
                    ssl_certfile=tls_items.get(TLS_CERT),
                    ssl_keyfile_password=password,
                    ssl_ca_certs=tls_items.get(CA_CERTS),
                    ssl_cert_reqs=ssl.CERT_REQUIRED,
                    ssl_ciphers='ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS'
                )
                config.load()
                logger.info('TLS configuration loaded successfully for server')
                CertUtil.secure_delete_password(password)
                password = None
            else:
                config = uvicorn.Config(self, reload=False)
            
            self._server = uvicorn.Server(config)
            ip = ipaddress.ip_address(ip)
            if ip.version == IPV4:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                logger.info(f"[node_manager Server][ipv4] Running at {http_type}://{ip}:{port}")
            elif ip.version == IPV6 and socket.has_ipv6:
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                logger.info(f"[node_manager Server][ipv6] Running at {http_type}://[{ip}]:{port}")
            else:
                raise Exception(f'[node_manager Server][{http_type}] POD_IP is not ipv4 or ipv6.')
            
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((str(ip), port))
            sock.listen(MAX_CONNECTED_CLIENT_NUM)

            self._server.run(sockets=[sock])
        finally:
            if sock:
                sock.close()
            if password:
                CertUtil.secure_delete_password(password)
                password = None

    def shutdown(self):
        if self._server:
            self._server.should_exit = True


app = Server()
