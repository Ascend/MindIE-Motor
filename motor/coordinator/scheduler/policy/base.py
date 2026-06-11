# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

from abc import ABC, abstractmethod

from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint
from motor.coordinator.domain import InstanceProvider
from motor.common.logger import get_logger

logger = get_logger(__name__)


class BaseSchedulingPolicy(ABC):
    """
    Abstract base class for all scheduler policies.
    Defines the interface that all scheduler policies must implement.
    Subclasses get instance list via _instance_provider, not directly from InstanceManager.
    """

    def __init__(self, instance_provider: InstanceProvider) -> None:
        self._instance_provider = instance_provider

    @abstractmethod
    def _select_instance(self, role: PDRole = None) -> Instance | None:
        """
        Select the best instance based on the scheduling algorithm.

        Args:
            role: Optional PDRole to filter instances by role (prefill/decode)

        Returns:
            Selected Instance or None if no instance available
        """
        raise NotImplementedError("Subclasses must implement select_instance method")

    @abstractmethod
    def _select_endpoint(self, instance: Instance) -> Endpoint | None:
        """
        Select the best endpoint from the given instance based on the scheduling algorithm.

        Args:
            instance: The instance to select an endpoint from

        Returns:
            Selected Endpoint or None if no endpoint available
        """
        raise NotImplementedError("Subclasses must implement select_endpoint method")

    def select_instance_and_endpoint(self, role: PDRole = None):
        """
        Select an instance and endpoint based on the current scheduling algorithm.

        Args:
            role: Optional PDRole to filter instances by role (prefill/decode)

        Returns:
            (Instance, Endpoint) tuple or None if no instance available
        """
        instance = self._select_instance(role)
        if instance is None:
            return None
        endpoint = self._select_endpoint(instance)
        if endpoint is None:
            return None
        return (instance, endpoint)

    def select_instance_and_endpoint_from_list(
        self,
        instances: list[Instance],
        role: PDRole | None = None,
        req_info=None,
    ):
        """Select from an already filtered instance subset."""
        del role, req_info
        for instance in instances:
            endpoint = self._select_endpoint(instance)
            if endpoint is not None:
                return (instance, endpoint)
        return None
