# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


import os
import threading
import time
import unittest

import requests

from node_manager import app


class TestServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        ms_node_manager = {
            "version": "v1.0",
            "controller_alarm_port": 1027,
            "node_manager_port": 1028,
            "heartbeat_interval_seconds": 5,
            "tls_config": {
                "server_tls_enable": False,
                "server_tls_items": {},
                "client_tls_enable": False,
                "client_tls_items": {}
            }
        }
        threading.Thread(target=app.run, kwargs={'ms_node_manager': ms_node_manager}).start()
        time.sleep(1)

    def test_run(self):
        try:
            url = 'http://127.0.0.1:1028/v1/node-manager/running-status'
            response = requests.get(url, timeout=3)
            self.assertLess(response.status_code, 500)
        except requests.RequestException:
            self.fail("Server is not running!")

    def test_shutdown(self):
        app.shutdown()
        time.sleep(1)
        self.assertTrue(app.should_exit)


if __name__ == "__main__":
    unittest.main()
