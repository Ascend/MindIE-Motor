# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import ctypes
import ipaddress
import sys
import threading
import time
from abc import ABC, abstractmethod

from ccae_reporter.backends import select_backend
from ccae_reporter.common.cert_util import AdapterCertUtil
from ccae_reporter.common.logging import Log
from ccae_reporter.common.util import format_address
from ccae_reporter.config import ConfigUtil
from ccae_reporter.reporters.kafka_client.kafka_produce import KafkaProducer

from motor.common.http.http_client import SafeHTTPSClient
from motor.config.tls_config import TLSConfig


def _format_remote_info(monitor_ip, log_ports):
    return ','.join([format_address(monitor_ip, port) for port in log_ports])


class BaseReporter(ABC):
    def __init__(self, backend_name: str, identity: str, *args, **kwargs):
        self.logger = Log(__name__).getlog()
        self.backend = select_backend(backend_name)(identity, *args, **kwargs)
        monitor_config = ConfigUtil.get_config("north_config")
        self.monitor_ip = monitor_config.get("ip", "")
        monitor_http_port = monitor_config.get("port", "")
        try:
            ipaddress.ip_address(self.monitor_ip)
        except ValueError as v_e:
            raise RuntimeError(f"Invalid CCAE IP: {self.monitor_ip}") from v_e
        except Exception as e:
            raise RuntimeError(f"Invalid CCAE IP: {self.monitor_ip}") from e
        if not 0 <= monitor_http_port <= 65535:
            raise RuntimeError(f"Invalid CCAE Port: {monitor_http_port}, should between 0 and 65535")

        self.logger.info(
            f"Detect monitor config successfully! monitor_ip = {self.monitor_ip}, "
            f"monitor_http_port = {monitor_http_port}"
        )
        self.url_prefix = format_address(self.monitor_ip, monitor_http_port)

        self.tls_config = ConfigUtil.get_config("motor_deploy_config.tls_config.north_tls_config")
        if self.tls_config and self.tls_config.get("enable_tls"):
            self.logger.info("Sending requests with ssl!")
        else:
            self.logger.info("Sending requests without ssl!")

        self.http_client = SafeHTTPSClient(
            address=self.url_prefix, protocol="https://", tls_config=TLSConfig.from_dict(self.tls_config)
        )

        self.heart_beat_ready = threading.Event()
        self.log_topic = None
        self.log_ports = None
        self.remote_info = None
        self.producer = None
        self.identity = identity
        self.alarm_to_send = []
        self.running = True

    def init_producer(self):
        kafka_config = {
            "bootstrap.servers": self.remote_info,
            "client.id": "python-producer",
            "acks": "all",
            "retries": 3,
            "compression.type": "snappy",
            "queue.buffering.max.messages": 100000,
            "queue.buffering.max.ms": 500,
            "batch.num.messages": 10000,
            "security.protocol": "PLAINTEXT",
        }

        if not self.tls_config or not self.tls_config.get("enable_tls"):
            self.logger.info("Sending kafka requests without ssl.")
            self.producer = KafkaProducer(kafka_config)
            return

        self.logger.info("Sending Kafka requests with ssl!")
        password = AdapterCertUtil.validate_cert_and_decrypt_password(self.tls_config)
        kafka_config.update(
            {
                "security.protocol": "ssl",
                "ssl.ca.location": self.tls_config["ca_file"],
                "ssl.certificate.location": self.tls_config["cert_file"],
                "ssl.key.location": self.tls_config["key_file"],
                "ssl.key.password": password,
            }
        )
        if self.tls_config["crl_file"]:
            kafka_config["ssl.crl.location"] = self.tls_config["crl_file"]
        self.producer = KafkaProducer(kafka_config)
        password_len = len(password)
        password_offset = sys.getsizeof(password) - password_len - 1
        ctypes.memset(id(password) + password_offset, 0, password_len)
        del password

    def run(self):
        heart_beater = threading.Thread(target=self.heart_beat)
        heart_beater.start()
        self.logger.info("Heartbeat thread starts successfully!")
        self.heart_beat_ready.wait()

        self.remote_info = _format_remote_info(self.monitor_ip, self.log_ports)
        self.init_producer()
        log_thread = threading.Thread(target=self.fetch_log_and_upload)
        log_thread.start()
        self.logger.info("Log monitor starts successfully!")

        if self.identity == "Coordinator":
            self.logger.info("Inventory and Alarm shouldn't be reported when identity is `Coordinator`.")
            return

        precision_thread = threading.Thread(target=self.run_precision_control_worker)
        precision_thread.start()
        self.logger.info("Precision control thread starts successfully!")

        alarm_thread = threading.Thread(target=self.fetch_alarm_info_and_upload)
        alarm_thread.start()
        self.logger.info("Alarm thread starts successfully!")

        inventory_thread = threading.Thread(target=self.fetch_inventory_info_and_upload)
        inventory_thread.start()
        self.logger.info("Inventory thread starts successfully!")

    def stop(self):
        self.running = False

    def precision_control_periodic(self) -> None:
        """Optional northbound precision control poll; overridden by CCAEReporter."""

    def run_precision_control_worker(self) -> None:
        while self.running:
            time.sleep(1)
            try:
                self.precision_control_periodic()
            except Exception as e:
                self.logger.error(e)

    @abstractmethod
    def send_heart_beat(self):
        pass

    @abstractmethod
    def upload_alarm(self, alarms: str) -> bool:
        pass

    @abstractmethod
    def upload_inventory(self, inventories: str):
        pass

    @abstractmethod
    def upload_log(self, log_request_message: dict):
        pass

    @abstractmethod
    def fetch_models_and_update(self) -> list:
        pass

    def heart_beat(self):
        while self.running:
            time.sleep(5)
            if self.backend.is_alive():
                self.logger.debug("Backend is alive!")
                try:
                    self.send_heart_beat()
                except Exception as e:
                    self.logger.error(e)
            else:
                self.logger.debug("Backend is not alive!")

    def fetch_alarm_info_and_upload(self):
        while self.running:
            time.sleep(1)
            total_alarms = self.alarm_to_send + self.backend.fetch_alarm_info()
            if not total_alarms:
                continue
            failed_alarms = []
            for item in total_alarms:
                if not item:
                    continue
                try:
                    if not self.upload_alarm(item):
                        failed_alarms.append(item)
                        self.logger.info(f"Retain alarms: {failed_alarms}")
                except Exception as e:
                    self.logger.error(e)
            self.alarm_to_send = failed_alarms

    def fetch_inventory_info_and_upload(self):
        while self.running:
            time.sleep(0.1)
            inventory_models = self.fetch_models_and_update()
            if not inventory_models:
                continue
            for model_id in inventory_models:
                res = self.backend.fetch_inventory_info(model_id)
                if not res:
                    continue
                try:
                    self.upload_inventory(res)
                except Exception as e:
                    self.logger.error(e)

    def fetch_log_and_upload(self):
        while self.running:
            time.sleep(3)
            try:
                log_request_message = self.backend.fetch_log_messages()
                if log_request_message:
                    self.upload_log(log_request_message)
            except Exception as e:
                self.logger.error(e)
