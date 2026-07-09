# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import queue
import threading
import time
from dataclasses import dataclass

from motor.common.resources import Instance, ReadOnlyInstance, InsEventMsg, EventType
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.config.controller import ControllerConfig
from motor.controller.api_client.coordinator_api_client import CoordinatorApiClient
from motor.controller.core import Observer, ObserverEvent

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)


@dataclass
class Event:
    event_type: EventType
    instance: Instance | None


class EventPusher(Observer):
    def __init__(self, config: ControllerConfig | None = None) -> None:
        super().__init__()
        # Use default config if not provided
        if config is None:
            self.config = ControllerConfig()
        else:
            self.config = config

        self.is_coordinator_reset = False
        self.is_first_heartbeat_success = False  # Track if we've ever successfully connected to coordinator
        self.event_queue = queue.Queue()
        self.instances: dict[str, Instance] = {}
        self.lock = threading.Lock()
        self.config_lock = threading.RLock()
        self.stop_event = threading.Event()

        # Condition variable for on-demand wake-up instead of busy-waiting.
        self.work_condition = threading.Condition()

        # Extract required config fields
        with self.config_lock:
            self.coordinator_heartbeat_interval = config.event_config.coordinator_heartbeat_interval
            self._set_sync_interval = config.event_config.coordinator_set_sync_interval

        # Track last periodic SET sync time (initialized to now so first sync fires after interval)
        self._last_set_sync_time = time.time()

        # Last successfully sent instance-ID fingerprint; used to suppress noisy periodic SET logs
        self._last_sent_fingerprint: tuple[int, ...] | None = None

        # Track whether we've already sent SET for the current ready=False period.
        # Prevents repeated SET pushes when Coordinator stays not-ready (e.g. only
        # decode instances remain after a prefill failure).
        self._ready_false_set_sent = False

        self.event_consumer_thread = None
        self.heartbeat_detector_thread = None

        logger.info("EventPusher initialized.")

    def start(self) -> None:
        """Start the event pusher threads"""
        # Reset stop_event if it was previously set (for singleton reuse)
        if self.stop_event.is_set():
            self.stop_event.clear()

        # Create event pusher threads
        self.event_consumer_thread = threading.Thread(target=self._event_consumer, daemon=True, name="EventConsumer")
        self.heartbeat_detector_thread = threading.Thread(
            target=self._coordinator_heartbeat_detector, daemon=True, name="HeartbeatDetector"
        )

        self.event_consumer_thread.start()
        self.heartbeat_detector_thread.start()
        logger.info("EventPusher started.")

    def stop(self) -> None:
        self.stop_event.set()
        with self.work_condition:
            self.work_condition.notify_all()
        # Only join threads that have been started
        if (
            hasattr(self, 'event_consumer_thread')
            and self.event_consumer_thread is not None
            and self.event_consumer_thread.is_alive()
        ):
            self.event_consumer_thread.join()
        if (
            hasattr(self, 'heartbeat_detector_thread')
            and self.heartbeat_detector_thread is not None
            and self.heartbeat_detector_thread.is_alive()
        ):
            self.heartbeat_detector_thread.join()
        if hasattr(self, 'heart_client'):
            self.heart_client.close()
        logger.info("EventPusher stopped.")

    def is_alive(self) -> bool:
        """Check if the event_pusher threads are alive"""
        return (
            self.event_consumer_thread is not None
            and self.event_consumer_thread.is_alive()
            and self.heartbeat_detector_thread is not None
            and self.heartbeat_detector_thread.is_alive()
        )

    def update_config(self, config: ControllerConfig) -> None:
        """Update configuration for the event pusher"""
        with self.config_lock:
            self.coordinator_heartbeat_interval = config.event_config.coordinator_heartbeat_interval
            self._set_sync_interval = config.event_config.coordinator_set_sync_interval

    def update(self, instance: ReadOnlyInstance, event: ObserverEvent) -> None:
        # Event pusher will interact with coordinator and send instances.
        # So it should just use Instance instead of ReadOnlyInstance.
        if event == ObserverEvent.INSTANCE_READY:
            with self.lock:
                self.instances[instance.job_name] = instance
            # Deep copy the instance to ensure data consistency during async HTTP sending
            event = Event(EventType.ADD, instance.to_instance())
            logger.info("Instance ready: %s", instance.job_name)
        elif event == ObserverEvent.INSTANCE_SEPERATED:
            with self.lock:
                if instance.job_name in self.instances:
                    del self.instances[instance.job_name]
                else:
                    # When instance abnormal in initial stage, we ignore this event
                    return
            # Deep copy the instance to ensure data consistency during async HTTP sending
            event = Event(EventType.DEL, instance.to_instance())
            logger.info("Instance removed: %s", instance.job_name)
        elif event == ObserverEvent.INSTANCE_PAUSED:
            with self.lock:
                if instance.job_name in self.instances:
                    del self.instances[instance.job_name]
                else:
                    return
            event = Event(EventType.PAUSE, instance.to_instance())
            logger.info("Instance paused: %s", instance.job_name)
        elif event == ObserverEvent.INSTANCE_RESUMED:
            with self.lock:
                self.instances[instance.job_name] = instance
            event = Event(EventType.RESUME, instance.to_instance())
            logger.info("Instance resumed: %s", instance.job_name)
        elif event == ObserverEvent.INSTANCE_REMOVED:
            event = Event(EventType.DEL, instance.to_instance())
            logger.info("Instance removed: %s", instance.job_name)
        else:
            # Other event we don't handle, just return
            return

        self.event_queue.put(event)

    def push_event(self, event_type: EventType) -> None:
        event = Event(event_type, None)
        self.event_queue.put(event)
        logger.info("Pushed event: %s", event_type)

    def _event_consumer(self) -> None:
        # Use get(timeout=1.0) to process events without forced throttling
        # while still checking stop_event every second when the queue is idle.
        while not self.stop_event.is_set():
            try:
                event = self.event_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if event is not None:
                event_type = event.event_type
                set_fingerprint: tuple | None = None
                if event_type == EventType.ADD:
                    event_msg = InsEventMsg(event=event_type, instances=[event.instance])
                elif event_type == EventType.DEL:
                    event_msg = InsEventMsg(event=event_type, instances=[event.instance])
                elif event_type == EventType.PAUSE:
                    event_msg = InsEventMsg(event=event_type, instances=[event.instance])
                elif event_type == EventType.RESUME:
                    event_msg = InsEventMsg(event=event_type, instances=[event.instance])
                elif event_type == EventType.SET:
                    with self.lock:
                        instances = list(self.instances.values())
                        set_fingerprint = tuple(sorted(inst.id for inst in instances))

                    if instances:
                        event_msg = InsEventMsg(
                            event=event_type, instances=[instance.to_instance() for instance in instances]
                        )
                    else:
                        logger.debug(
                            "SET event skipped: no instances in memory (Controller may have lost its own instances)."
                        )
                        event_msg = None
                else:
                    logger.error("Unknown event type: %s", event_type)
                    continue

                if event_msg is not None:
                    try:
                        CoordinatorApiClient.send_instance_refresh(event_msg)
                        if event_type == EventType.SET:
                            self._last_sent_fingerprint = set_fingerprint
                    except Exception as e:
                        logger.error("Failed to send instance refresh event, error: %s", e)

    def _coordinator_heartbeat_detector(self) -> None:
        """
        Detect Coordinator heartbeat, when Coordinator need Controller sent all
        instances resource, this function will produce a SET event.
        """
        hb_loss_cnt = 0  # Consecutive heartbeat failures (path 2 debounce)
        not_ready_log_interval = 12  # Only log "not ready" every 12 iterations
        not_ready_log_counter = 0

        while not self.stop_event.is_set():
            try:
                params = {"status": "normal"}
                response = CoordinatorApiClient.query_status(params)
                # Mark that we've successfully connected to coordinator at least once
                if not self.is_first_heartbeat_success:
                    self.is_first_heartbeat_success = True
                    logger.info("Coordinator heartbeat established successfully.")
                    not_ready_log_counter = 0

                if response is None or response.get("ready") is None or not response.get("ready"):
                    # When get info 'coordinator is not ready', controller will reset coordinator
                    # Only log not ready message periodically to avoid spam
                    not_ready_log_counter += 1
                    if not_ready_log_counter >= not_ready_log_interval:
                        logger.info("Coordinator is alive but is not ready.")
                        not_ready_log_counter = 0
                    # Only trigger SET on the first not-ready detection — not every
                    # iteration.  This avoids repeated SET pushes when Coordinator
                    # stays not-ready (e.g. only decode instances remain after a
                    # prefill failure).  The flag is reset when ready becomes True.
                    if not self._ready_false_set_sent:
                        self.is_coordinator_reset = True
                        self._ready_false_set_sent = True
                else:
                    self._ready_false_set_sent = False

                if self.is_coordinator_reset:
                    # SET event means push all instances to coordinator,
                    # so job_name is not a instance job_name, it is "coordinator_restart".
                    event = Event(EventType.SET, None)
                    self.event_queue.put(event)
                    self.is_coordinator_reset = False
                    hb_loss_cnt = 0
                    logger.debug("Controller will reset coordinator instance info.")

            except Exception as e:
                # Only count heartbeat loss after we've successfully connected at least once
                if self.is_first_heartbeat_success:
                    hb_loss_cnt += 1
                    if hb_loss_cnt >= 2:
                        self.is_coordinator_reset = True
                        logger.warning("Coordinator heartbeat lost. Possible restart detected.")
                        hb_loss_cnt = 0
                    # Rate-limit repeated connection-failure logs via error_window.
                    _rl.error_window(
                        "event_pusher.coordinator_hb_fail",
                        "Send Coordinator heartbeat failed, Exception occurred %s" % e,
                        window_sec=60,
                        level="WARNING",
                    )
                else:
                    # Rate-limit the "not yet available" message during initial startup.
                    _rl.error_window(
                        "event_pusher.coordinator_not_available",
                        "Coordinator not yet available, waiting for first successful heartbeat.",
                        window_sec=60,
                        level="INFO",
                    )

            # Periodic full-instance SET sync to coordinator (fallback for controller restart / missed events)
            if self._set_sync_interval > 0:
                now = time.time()
                if now - self._last_set_sync_time >= self._set_sync_interval:
                    event = Event(EventType.SET, None)
                    self.event_queue.put(event)
                    self._last_set_sync_time = now
                    with self.lock:
                        fingerprint = tuple(sorted(inst.id for inst in self.instances.values()))
                    if fingerprint != self._last_sent_fingerprint:
                        logger.info("Periodic SET sync triggered (interval=%ds)", self._set_sync_interval)

            with self.config_lock:
                heartbeat_interval = self.coordinator_heartbeat_interval
            with self.work_condition:
                self.work_condition.wait(timeout=heartbeat_interval)
