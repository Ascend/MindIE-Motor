# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Persistence mixin for FaultManager: ETCD save/restore with version control.

All attributes referenced via self (nodes, instances, lock, etcd_client, etc.)
are provided by FaultManager.__init__, not redeclared here.
"""

import time
import threading

from motor.common.logger import get_logger
from motor.common.etcd.persistent_state import PersistentState
from motor.controller.fault_tolerance.fault_types import InstanceMetadata, NodeMetadata

logger = get_logger(__name__)


class _PersistenceMixin:
    """ETCD persistence for FaultManager nodes and instances.

    Serializes self.nodes and self.instances to ETCD under
    /controller/fault_manager with monotonic versioning and checksum validation.
    Called after node/instance state changes, strategy creation/completion.
    """

    def persist_data(self) -> bool:
        """Serialize nodes and instances to ETCD with version and checksum.

        Triggered after:
        - Node or instance status changes (faults added/removed).
        - Strategy is created or updated.
        - Strategy completes and faults are cleared.

        Returns True on success, False on any error (ETCD unavailable, etc.).
        """
        try:
            with self.lock:
                current_time = time.time()
                next_version = self._get_next_version()

                fault_data = {"nodes": {}, "instances": {}}
                for node_name, node_metadata in self.nodes.items():
                    fault_data["nodes"][node_name] = node_metadata.model_dump(mode="json")
                for ins_id, ins_metadata in self.instances.items():
                    fault_data["instances"][str(ins_id)] = ins_metadata.model_dump(mode="json")
                logger.debug("Persisting fault manager data - full data: %s", fault_data)

                persistent_state = PersistentState(
                    data=fault_data,
                    version=next_version,
                    timestamp=current_time,
                    checksum="",
                )
                persistent_state.checksum = persistent_state.calculate_checksum()
                logger.debug(
                    "Persisting fault manager data - calculated checksum: %s, version: %s, timestamp: %s",
                    persistent_state.checksum,
                    next_version,
                    current_time,
                )

                dict_data = {"state": persistent_state.model_dump()}
                logger.debug("Persistence data being saved to ETCD: %s", dict_data)

            # Release lock before ETCD I/O to avoid blocking other operations
            success = self.etcd_client.persist_data("/controller/fault_manager", dict_data)
            if success:
                logger.info("Successfully persisted fault manager data with version %d", next_version)
            return success
        except Exception as e:
            logger.error("Error persisting fault manager data: %s", e)
            return False

    def restore_data(self) -> bool:
        """Restore nodes and instances from ETCD into self.nodes/self.instances.

        Called during FaultManager.start(). Reads the persisted state from
        /controller/fault_manager, validates the checksum, and repopulates
        self.nodes and self.instances. The data version is advanced to at
        least the restored version to avoid collisions on the next persist.

        Returns True on success or if no data exists (fresh start). Returns
        False on data corruption or ETCD errors.
        """
        try:
            persistent_states = self.etcd_client.restore_data("/controller/fault_manager", PersistentState)
            if persistent_states is None:
                logger.info("No fault manager data found in ETCD, starting with empty state")
                return True

            logger.info("Restoring fault manager data from ETCD")

            persistent_state = persistent_states.get("state")
            if persistent_state is None:
                logger.warning(
                    "Expected 'state' key not found in persistent states, found keys: %s",
                    list(persistent_states.keys()),
                )
                return False

            if not isinstance(persistent_state, PersistentState):
                logger.error("Invalid persistent state format, expected PersistentState instance")
                return False

            if not persistent_state.is_valid():
                logger.error("Data integrity check failed for fault_manager, cannot restore")
                return False

            self._ensure_version_init()
            self._data_version = max(self._data_version, persistent_state.version)
            with self.lock:
                self.nodes.clear()
                self.instances.clear()

                nodes_data = persistent_state.data.get("nodes", {})
                for node_name, node_dict in nodes_data.items():
                    self.nodes[node_name] = NodeMetadata.model_validate(node_dict)

                instances_data = persistent_state.data.get("instances", {})
                for ins_id_str, ins_dict in instances_data.items():
                    ins_metadata = InstanceMetadata.model_validate(ins_dict)
                    self.instances[ins_metadata.instance_id] = ins_metadata
                    logger.debug("Restored instance %s", ins_id_str)

            logger.info(
                "Successfully restored fault manager data: %d nodes, %d instances",
                len(self.nodes),
                len(self.instances),
            )
            return True
        except Exception as e:
            logger.error("Error restoring fault manager data: %s", e)
            return False

    def _get_next_version(self) -> int:
        """Get next data version for persistence"""
        self._ensure_version_init()
        with self._version_lock:
            self._data_version += 1
            return self._data_version

    def _ensure_version_init(self) -> None:
        """Lazily initialize version-control attributes if not set by the owning class."""
        if not hasattr(self, "_data_version"):
            self._data_version = 0
        if not hasattr(self, "_version_lock"):
            self._version_lock = threading.Lock()
