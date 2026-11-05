# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import json
import logging
import socket
import sys

import requests

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    filename="/mnt/cache/health_check_probe.log",
    filemode="a",
)

if __name__ == "__main__":
    # only master node has this resource. For slave node, do not check vllm process.
    hostname = socket.gethostname()
    if "head" not in hostname:
        logging.debug(f"node {hostname} is not head, do not need probe")
        sys.exit(0)
    logging.info("health check start")

    local_ip = socket.gethostbyname(hostname)
    api_url = f"http://{local_ip}:1025/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
    }
    request_data = {
        "model": "glm51",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "max_tokens": 2,
        "temperature": 0.6,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        response = requests.post(api_url, json=request_data, headers=headers, stream=False, timeout=1200)
    except Exception as e:
        logging.error(f"requests post failed, Exception: {e}")
        sys.exit(1)

    if response.status_code != 200:
        logging.error(f"Response error, status code: {response.status_code}, text: {response.text}")
        sys.exit(1)

    try:
        response_info = json.loads(response.text)
        if len(response_info["choices"][0]["message"]["content"]) == 0:
            logging.error("response content len is 0")
            sys.exit(1)
    except Exception as e:
        logging.error(f"json parse failed, text: {response.text}, Exception: {e}")
        sys.exit(1)

    logging.info(f"health check success, response: {response.text}")
