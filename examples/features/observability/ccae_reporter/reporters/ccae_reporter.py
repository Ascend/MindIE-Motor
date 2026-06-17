# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import http.server
import json
import os
import re
import socketserver
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from ccae_reporter.common.util import safe_open
from ccae_reporter.thread_safe_util import ThreadSafeFactory
from ccae_reporter.reporters.base_reporter import BaseReporter

import motor

CATEGORY_STR = "category"
ALARM_ID_STR = "alarmId"
MODEL_ID_STR = "modelID"
INVENTORIES_STR = "inventories"
METRICS_STR = "metrics"

PRECISION_CONTROL_URL = "/rest/ccaeommgmt/v1/managers/mindie/precisioncontrol"
PRECISION_COMMAND_DETECTION = "precision_detection"

DEBUG_HOOK_ENABLED_ENV = "CCAE_DEBUG_HOOK_ENABLED"
DEBUG_HOOK_PORT_ENV = "CCAE_DEBUG_HOOK_PORT"
DEBUG_HOOK_BIND_ENV = "CCAE_DEBUG_HOOK_BIND"
DEBUG_HOOK_DEFAULT_PORT = 9999
DEBUG_HOOK_DEFAULT_BIND = "127.0.0.1"


class _PrecisionDebugHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler exposing precision control debug endpoints.

    The owning reporter is bound to the class attribute below by
    :meth:`CCAEReporter._maybe_start_debug_hook`. Using a class attribute keeps
    the handler stateless across requests while still reaching the live
    reporter instance.
    """

    reporter: "CCAEReporter | None" = None

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        if self.reporter is not None:
            self.reporter.logger.debug("precision debug http: " + format, *args)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/debug/precision_tasks":
            self._list_tasks()
        elif path == "/debug/health":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/debug/trigger_precision":
            self._trigger_precision()
        else:
            self._send_json(404, {"error": "not found", "path": path})

    def _list_tasks(self):
        if self.reporter is None:
            self._send_json(503, {"error": "reporter not bound"})
            return
        with self.reporter._precision_lock:
            tasks = {
                mid: {"control_code": t.control_code, "status": t.status}
                for mid, t in self.reporter._precision_tasks.items()
            }
        self._send_json(200, {"tasks": tasks})

    def _trigger_precision(self):
        if self.reporter is None:
            self._send_json(503, {"error": "reporter not bound"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            item = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"invalid json: {e}"})
            return
        if not isinstance(item, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return
        self.reporter._apply_req_list_item(item)
        mid = item.get(MODEL_ID_STR)
        with self.reporter._precision_lock:
            t = self.reporter._precision_tasks.get(mid) if isinstance(mid, str) else None
            task_state = {"control_code": t.control_code, "status": t.status} if t else None
        self._send_json(200, {"mid": mid, "task": task_state})

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@dataclass
class _PrecisionControlTask:
    """Per-model precision control task whose ``status`` reflects actual termination result.

    ``switchControl`` / ``immediateDelivery`` are stored for a future phase that
    wires runtime precision detection inside Coordinator; they do not change runtime yet.
    """

    control_code: str
    status: str = "Initial"  # Initial | Completed
    last_precision_command: str | None = None
    last_switch_control: str | None = None
    last_immediate_delivery: bool | None = None


def response_raise_for_status(response, interface_name: str):
    if response.status_code >= 400:
        raise RuntimeError(
            f"Response from {interface_name} failed, status is {response.status_code}, content is {response.text}"
        )


def check_element(item: dict, key: str):
    if key not in item.keys():
        raise ValueError(f"Failed to read http response, lack key `{key}`")


class CCAEReporter(BaseReporter):
    def __init__(self, backend_name: str, identity: str):
        super().__init__(backend_name, identity)
        # model_id_period 值为一个三元list
        # 第一位 bool 代表是否需要立即上报
        # 第二位 int 代表上报的时间间隔，以秒为单位
        # 第三位 float 代表上一次上报的时间戳，以秒为单位
        self.model_id_period = ThreadSafeFactory.make_threadsafe_instance(dict)
        self.alarm_cache = ThreadSafeFactory.make_threadsafe_instance(dict)
        for _ in range(1):
            model_id = os.getenv("SERVICE_ID")
            if model_id is None:
                raise RuntimeError("Environment variable $SERVICE_ID is not set.")
            max_env_len = 256
            if len(model_id) > max_env_len:
                raise RuntimeError("Environment variable $SERVICE_ID is not correct.")
            self.model_id_period[model_id] = [False, 1, time.time()]
        self.version = self.fetch_version_info()
        self.component_type = -1
        if identity == "Coordinator":
            self.component_type = 1
        elif identity == "Controller":
            self.component_type = 0
        self._precision_lock = threading.Lock()
        self._precision_tasks: dict[str, _PrecisionControlTask] = {}
        self._maybe_start_debug_hook()

    def _maybe_start_debug_hook(self) -> None:
        """Start a debug HTTP server when ``CCAE_DEBUG_HOOK_ENABLED`` is truthy.

        Exposes:
          - ``POST /debug/trigger_precision``: feed a fake ``reqList`` item directly
            into :meth:`_apply_req_list_item`. Body is a JSON object with the same
            shape as a CCAE response entry (``modelID``, ``precisionCommand``,
            ``controlCode``, ``controlStatusRespond``, ``inferencePrecisionControl``).
          - ``GET /debug/precision_tasks``: dump current ``_precision_tasks`` state.
          - ``GET /debug/health``: liveness probe.

        Defaults to binding ``127.0.0.1`` to avoid exposing the hook to the
        network; override with ``CCAE_DEBUG_HOOK_BIND``. Port defaults to 9999
        and is configurable via ``CCAE_DEBUG_HOOK_PORT``.
        """
        flag = os.getenv(DEBUG_HOOK_ENABLED_ENV, "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return
        try:
            port = int(os.getenv(DEBUG_HOOK_PORT_ENV, str(DEBUG_HOOK_DEFAULT_PORT)))
        except ValueError:
            self.logger.error(
                "precision debug hook: invalid %s, expected int, hook disabled",
                DEBUG_HOOK_PORT_ENV,
            )
            return
        bind = os.getenv(DEBUG_HOOK_BIND_ENV, DEBUG_HOOK_DEFAULT_BIND)

        _PrecisionDebugHandler.reporter = self
        try:
            server = _ThreadedHTTPServer((bind, port), _PrecisionDebugHandler)
        except OSError as e:
            self.logger.error(
                "precision debug hook: failed to bind %s:%d (%s), hook disabled",
                bind,
                port,
                e,
            )
            return
        thread = threading.Thread(target=server.serve_forever, name="precision-debug-hook", daemon=True)
        thread.start()
        self.logger.info(
            "precision debug hook ENABLED: bind=%s port=%d"
            " endpoints=POST /debug/trigger_precision, GET /debug/precision_tasks, GET /debug/health",
            bind,
            port,
        )

    def _model_name(self) -> str:
        return os.getenv("MODEL_NAME") or ""

    @staticmethod
    def _parse_instance_ids_from_control_code(code: str) -> tuple[int, int]:
        """Parse P/D instance IDs from controlCode format ``...-pId=m-dId=n``."""
        m = re.search(r"pId=(\d+)-dId=(\d+)", code)
        if not m:
            return 0, 0
        return int(m.group(1)), int(m.group(2))

    def _apply_req_list_item(self, item: dict) -> None:
        """Update state from one ``reqList`` entry (after a successful POST)."""
        if not isinstance(item, dict):
            self.logger.warning("precision reqList entry is not dict: %r", item)
            return
        mid = item.get(MODEL_ID_STR)
        if not mid or not isinstance(mid, str):
            self.logger.warning("precision reqList entry missing valid modelID: %r", item)
            return

        self.logger.info(
            "precision reqList item: modelID=%s hasControlStatusRespond=%s precisionCommand=%s hasControlCode=%s",
            mid,
            item.get("controlStatusRespond"),
            item.get("precisionCommand"),
            bool(item.get("controlCode")),
        )

        if item.get("controlStatusRespond") is True:
            with self._precision_lock:
                self._precision_tasks.pop(mid, None)
            self.logger.info(
                "precision task removed due to controlStatusRespond for modelID=%s, remaining tasks=%s",
                mid,
                list(self._precision_tasks.keys()),
            )
            return

        p_cmd = item.get("precisionCommand")
        code = item.get("controlCode")

        if p_cmd is not None and p_cmd != PRECISION_COMMAND_DETECTION:
            self.logger.warning(
                "precision control item has unknown precisionCommand=%r modelID=%s",
                p_cmd,
                mid,
            )

        if not code:
            if p_cmd == PRECISION_COMMAND_DETECTION:
                self.logger.warning(
                    "precision_detection without controlCode for modelID=%s",
                    mid,
                )
            return

        ipc = item.get("inferencePrecisionControl")
        switch_ctrl = None
        immediate = None
        if isinstance(ipc, dict):
            switch_ctrl = ipc.get("switchControl")
            immediate = ipc.get("immediateDelivery")

        p_id, d_id = self._parse_instance_ids_from_control_code(str(code))

        with self._precision_lock:
            existing = self._precision_tasks.get(mid)
            if existing and existing.control_code == str(code):
                if existing.status == "Completed":
                    self.logger.info(
                        "precision task already completed for controlCode=%s modelID=%s, updating fields only",
                        code,
                        mid,
                    )
                    if switch_ctrl is not None:
                        existing.last_switch_control = str(switch_ctrl)
                    if immediate is not None:
                        existing.last_immediate_delivery = bool(immediate)
                    existing.last_precision_command = str(p_cmd)
                    return
                self.logger.info(
                    "precision task retrying termination: modelID=%s controlCode=%s status=%s",
                    mid,
                    code,
                    existing.status,
                )
            else:
                mid_in_period = mid in self.model_id_period
                self._precision_tasks[mid] = _PrecisionControlTask(
                    control_code=str(code),
                    status="Initial",
                    last_precision_command=str(p_cmd),
                    last_switch_control=str(switch_ctrl) if switch_ctrl is not None else None,
                    last_immediate_delivery=bool(immediate) if immediate is not None else None,
                )
                self.logger.info(
                    "precision task CREATED: modelID=%s controlCode=%s"
                    " modelID_in_model_id_period=%s"
                    " model_id_period_keys=%s"
                    " parsed_p_id=%s parsed_d_id=%s",
                    mid,
                    code,
                    mid_in_period,
                    list(self.model_id_period.keys()),
                    p_id,
                    d_id,
                )

        if not d_id:
            self.logger.warning(
                "CCAE precision control: controlCode=%s has no valid instance IDs, modelID=%s",
                code,
                mid,
            )
            return

        if self.identity != "Controller":
            self.logger.warning(
                "CCAE precision control: termination skipped, identity=%s is not Controller, modelID=%s",
                self.identity,
                mid,
            )
            return

        self.logger.warning(
            "CCAE precision control: terminating D instance_id=%s p_instance_id=%s modelID=%s"
            " (via HTTP to Controller /controller/terminate_instance)",
            d_id,
            p_id,
            mid,
        )
        d_ok = self.backend.terminate_instance(d_id, "ccae_precision_control")
        p_ok = True
        if p_id:
            p_ok = self.backend.terminate_instance(p_id, "ccae_precision_control")

        with self._precision_lock:
            task = self._precision_tasks.get(mid)
            if task and task.control_code == str(code):
                task.status = "Completed" if (d_ok and p_ok) else "Initial"
                self.logger.info(
                    "precision termination result: modelID=%s d_id=%s p_id=%s d_ok=%s p_ok=%s status=%s",
                    mid,
                    d_id,
                    p_id,
                    d_ok,
                    p_ok,
                    task.status,
                )

    def _parse_precision_response(self, data: dict) -> None:
        if not isinstance(data, dict):
            self.logger.warning("precision response is not dict: %r", data)
            return
        req_list = data.get("reqList")
        if not isinstance(req_list, list):
            self.logger.info("precision response has no reqList or reqList is not list: %r", data)
            return
        self.logger.debug(
            "precision response reqList length=%d, entries=%s",
            len(req_list),
            [
                {
                    MODEL_ID_STR: e.get(MODEL_ID_STR),
                    "precisionCommand": e.get("precisionCommand"),
                    "hasControlCode": bool(e.get("controlCode")),
                    "controlStatusRespond": e.get("controlStatusRespond"),
                }
                for e in req_list
                if isinstance(e, dict)
            ],
        )
        for entry in req_list:
            self._apply_req_list_item(entry)

    def send_precision_control(self, request_data: dict) -> dict | None:
        """POST precision control body to CCAE; return response JSON on success, else None."""
        url = PRECISION_CONTROL_URL
        try:
            self.logger.debug("Sending precision control to %s with data: %s", url, request_data)
            response = self.http_client.do_post(url, request_data)
            response_raise_for_status(response, "precisioncontrol")
            response_json = response.json()
            self.logger.debug("Response from precision control is: %s", response_json)
        except Exception as e:
            self.logger.error("precisioncontrol request failed: %s", e)
            return None
        if not isinstance(response_json, dict):
            self.logger.error("precisioncontrol: invalid JSON body")
            return None
        if response_json.get("retCode") != 0:
            self.logger.error(
                "precisioncontrol retCode=%s retMsg=%s",
                response_json.get("retCode"),
                response_json.get("retMsg"),
            )
            return None
        return response_json

    def precision_control_periodic(self) -> None:
        if self.identity != "Controller":
            return
        if not self.backend.is_alive():
            return

        with self._precision_lock:
            self.logger.debug(
                "precision heartbeat tick: model_id_period_keys=%s precision_task_keys=%s",
                list(self.model_id_period.keys()),
                list(self._precision_tasks.keys()),
            )
            model_infos: list[dict] = []
            for model_id, _ in self.model_id_period.items():
                task = self._precision_tasks.get(model_id)
                if task is None:
                    self.logger.debug(
                        "precision body: modelID=%s has NO task, sending bare entry",
                        model_id,
                    )
                    model_infos.append(
                        {
                            MODEL_ID_STR: model_id,
                            "modelName": self._model_name(),
                        }
                    )
                else:
                    self.logger.debug(
                        "precision body: modelID=%s HAS task controlCode=%s controlStatus=%s",
                        model_id,
                        task.control_code,
                        task.status,
                    )
                    model_infos.append(
                        {
                            MODEL_ID_STR: model_id,
                            "modelName": self._model_name(),
                            "controlCode": task.control_code,
                            "controlStatus": task.status,
                        }
                    )
            request_data = {
                "timeStamp": int(time.time() * 1000),
                "modelServiceInfo": model_infos,
            }

        response_json = self.send_precision_control(request_data)
        if response_json is None:
            return

        self._parse_precision_response(response_json)

    def fetch_version_info(self) -> str:
        # Fetch version information from version.info file
        server_dir = os.path.dirname(motor.__file__)
        if server_dir is None:
            raise RuntimeError("Environment variable $MOTOR_INSTALL_PATH is not set.")
        with safe_open(os.path.join(server_dir, "version.info")) as f:
            for line in f:
                if "motor_version" in line:
                    return line.split(":")[-1].strip()
        self.logger.error("Failed to fetch version info.")
        return "UNKNOWN VERSION"

    def send_heart_beat(self):
        url = "/rest/ccaeommgmt/v1/managers/mindie/register"
        request_data = {
            "timeStamp": int(time.time() * 1000),
            "modelServiceInfo": [],
            "componentType": self.component_type,
            "version": self.version,
        }
        for model_id, _ in self.model_id_period.items():
            request_data["modelServiceInfo"].append(
                {
                    MODEL_ID_STR: model_id,
                    "modelName": os.getenv("MODEL_NAME"),
                }
            )
        try:
            self.logger.debug(f"Sending heartbeat to {url} with data: {request_data}")
            response = self.http_client.do_post(url, request_data)
            response_raise_for_status(response, "heartbeat")
            self.logger.debug("Response from heartbeat is: %s", response.json())
        except Exception as e:
            self.heart_beat_ready.clear()
            self.logger.error(e)
            return
        response_json = response.json()
        if response_json["retCode"] != 0:
            raise RuntimeError(f"Failed to send heartbeat! Return message from ccae is: {response_json['retMsg']}")
        check_element(response_json, "reqList")
        for req in response_json["reqList"]:
            check_element(req, MODEL_ID_STR)
            model_id = req[MODEL_ID_STR]
            check_element(req, INVENTORIES_STR)
            check_element(req[INVENTORIES_STR], "forceUpdate")
            check_element(req, METRICS_STR)
            check_element(req[METRICS_STR], "metricPeriod")
            self.model_id_period[model_id][0] = req[INVENTORIES_STR]["forceUpdate"]
            self.model_id_period[model_id][1] = req[METRICS_STR]["metricPeriod"]
            self.log_topic = req["logsServer"]["topic"]
            self.log_ports = req["logsServer"]["servicePort"]
        self.heart_beat_ready.set()

    def fetch_models_and_update(self) -> list:
        models_to_upload = []
        for model_id, send_tuple in self.model_id_period.items():
            if not send_tuple[0] and time.time() < send_tuple[1] + send_tuple[2]:
                continue
            self.model_id_period[model_id][0] = False
            self.model_id_period[model_id][2] = time.time()
            models_to_upload.append(model_id)
        return models_to_upload

    def upload_alarm(self, alarms) -> bool:
        for item in alarms:
            if CATEGORY_STR not in item:
                raise ValueError(f"Failed to send alarms, lack key `{CATEGORY_STR}`")
            if ALARM_ID_STR not in item:
                raise ValueError(f"Failed to send alarms, lack key `{ALARM_ID_STR}`")
            # a new alarm
            if item[CATEGORY_STR] == 1:
                self.alarm_cache[item[ALARM_ID_STR]] = item
            # cancel an alarm
            elif item[CATEGORY_STR] == 2:
                if item[ALARM_ID_STR] in self.alarm_cache.keys():
                    del self.alarm_cache[item[ALARM_ID_STR]]
        self.logger.info("Uploading alarms: %s", alarms)
        url = "/rest/ccaeommgmt/v1/managers/mindie/events"
        try:
            response = self.http_client.do_post(url, alarms)
            response_raise_for_status(response, "alarm")
            response_json = response.json()
            self.logger.debug("Response from alarm is: %s", response_json)
            return True
        except Exception as e:
            self.logger.error("Failed to upload alarms, error: %s", e)
            return False

    def upload_inventory(self, inventories):
        self.logger.debug(f"Uploading inventory: {inventories}")
        url = "/rest/ccaeommgmt/v1/managers/mindie/inventory"
        try:
            response = self.http_client.do_post(url, inventories)
            response_raise_for_status(response, "inventory")
            self.logger.debug("Response from inventory is: %s", response.json())
        except Exception as e:
            self.logger.error(f"Failed to upload inventory, error: {e}")

    def upload_log(self, log_request_message: dict):
        try:
            self.producer.send(self.log_topic, log_request_message)
        except Exception as e:
            self.logger.error(e)

    def fetch_alarm_cache(self) -> list:
        return list(self.alarm_cache.values())
