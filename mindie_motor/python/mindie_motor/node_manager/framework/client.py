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

import requests
from requests.adapters import HTTPAdapter

from node_manager.common.logging import Log
from node_manager.framework.utils import CertUtil

SUCCESS = 'success'
MSG = 'msg'
GET = 'GET'
POST = 'POST'
PUT = 'PUT'


class Client:
    def __init__(self, retry_times: int = 3, timeout: int = 3, headers: dict = None):
        self.setup_logging()

        self.retry_times = retry_times
        self.timeout = timeout
        self.headers = headers or {}
        self.context = None
        self.engine_map = dict()
        self.protocol = 'http://'

        # 从GeneralConfig获取engine的ip和port及controller的ip和port
        from node_manager.core.config import GeneralConfig
        self.config = GeneralConfig()
        self.engine_ip = self.config.get_server_engine_ip()
        for index, engine_port in enumerate(self.config.get_server_engine_port_list()):
            self.engine_map[index] = engine_port

        ms_node_manager = self.config.http_server_config
        self.enable_tls_verify = ms_node_manager.get('tls_config', {}).get('client_tls_enable', True)
        if self.enable_tls_verify:
            self.protocol = 'https://'
            self._initialize_tls_context(ms_node_manager)
            self.logger.info('node_manager client enable tls verify')
    
    @staticmethod
    def build_url(protocol: str, host: str, port, path: str = '') -> str:
        """Build URLwith format of http://host:port/path. IPv6 addresses are wrapped in square brackets per RFC 3986."""
        try:
            addr = ipaddress.ip_address(host)
            if addr.version == 6:
                host = f'[{host}]'
        except ValueError:
            # 解析IP地址失败, 则将host作为主机名构建url
            pass
        return f'{protocol}{host}:{port}{path}'

    def setup_logging(self):
        self.log_instance = Log(__name__)
        self.logger = self.log_instance.getlog()

    def update_engine_map(self, engine_map: dict):
        for index, port in engine_map.items():
            self.engine_map[index] = port

    def send_cmd_to_engine(self, engine_index: int, cmd: int) -> dict:
        port = self._get_engine_port(engine_index)
        if not port:
            return {SUCCESS: False, MSG: f'do not find ip or port with index({engine_index})'}
        url = self.build_url(self.protocol, self.engine_ip, port, '/v1/engine-server/fault-handling-command')
        data = {
            'cmd': cmd
        }
        return self._send_request(POST, url, data=data)

    def get_ep_status(self, engine_index: int) -> dict:
        port = self._get_engine_port(engine_index)
        if not port:
            return {SUCCESS: False, MSG: f'do not find ip or port with index({engine_index})'}
        url = self.build_url(self.protocol, self.engine_ip, port, '/v1/engine-server/running-status')
        return self._send_request(GET, url)

    def send_alarm_info_to_ctrler(self, error_info: list) -> dict:
        ip = self.config.get_controller_ip()
        port = self.config.get_controller_port()
        if not ip or not port:
            self.logger.error("controller's ip or port not exist")
            return {SUCCESS: False, MSG: "controller's ip or port not exist"}

        for idx, ep_errors in enumerate(error_info):
            ep_str = self._get_endpoint_string(idx)
            for error in ep_errors:
                if isinstance(error, dict) and 'errorLocation' not in error:
                    error['errorLocation'] = ep_str
            
        url = self.build_url(self.protocol, ip, port, '/v1/alarm/llm_engine')
        report_data = {
            'alarm_info': error_info,
            'node_manager_ip': self.engine_ip
        }
        return self._send_request(POST, url, data=report_data)

    def _initialize_tls_context(self, ms_node_manager: dict):
        password = None
        try:
            tls_items = ms_node_manager.get('tls_config', {}).get('client_tls_items', {})
            # 效验证书并解密密码
            password = CertUtil.validate_cert_and_decrypt_password(tls_items)
            # 创建SSL上下文（此方法内部会在使用password_bytes后立即清零）
            self.context = CertUtil.create_ssl_context(tls_items, password)
            self.logger.info('TLS context initialized successfully')
        except Exception as e:
            self.logger.error(f'Failed to initialize TLS context: {e}')
            raise
        finally:
            # 确保密码在使用后立即从内存中安全删除
            if password:
                CertUtil.secure_delete_password(password)
                password = None

    def _get_engine_port(self, engine_index: int):
        port = self.engine_map.get(engine_index, '')
        if not self.engine_ip:
            self.logger.error('engine_ip not exist')
            return None
        if not port:
            self.logger.error(f'do not find port with index({engine_index})')
            return None
        return port

    def _get_endpoint_string(self, engine_index: int) -> str:
        port = self._get_engine_port(engine_index)
        if self.engine_ip and port:
            return self.build_url('', self.engine_ip, port)
        return f'index={engine_index}'

    def _send_request(self, method: str, url: str, data=None) -> dict:
        method = method.upper()
        if data and method in [POST, PUT]:
            headers = self.headers.copy()
            headers["Content-Type"] = "application/json"
        else:
            headers = self.headers.copy()
        try:
            session = requests.Session()
            adapter = HTTPAdapter(max_retries=self.retry_times)
            adapter.init_poolmanager(
                connections=10,
                ssl_context=self.context,
                maxsize=10,
            )
            session.mount(self.protocol, adapter)
            response = session.request(
                method=method, url=url,
                headers=headers, json=data,
                timeout=self.timeout, verify=self.enable_tls_verify)
            response.raise_for_status()
            data = response.json()
            return {
                SUCCESS: True,
                "data": data,
            }
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"warn occurred during the request: {e}, url : {url}")
            return {
                SUCCESS: False,
                MSG: f"error occurred during the request: {e}, url : {url}",
            }
        except (KeyError, ValueError) as e:
            self.logger.error(f"error occurred while processing the response: {e}, url : {url}")
            return {
                SUCCESS: False,
                MSG: f"error occurred while processing the response: {e}, url : {url}",
            }
        except Exception as e:
            self.logger.error(f"unexpected error occurred: {e}, url : {url}")
            return {
                SUCCESS: False,
                MSG: f"unexpected error occurred: {e}, url : {url}",
            }
        finally:
            if session:
                session.close()
