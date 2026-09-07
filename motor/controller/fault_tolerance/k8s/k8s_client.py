# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from kubernetes import client, config

from motor.common.logger import get_logger

logger = get_logger(__name__)


class K8sClient:
    """Kubernetes client wrapper for common operations"""

    def __init__(self):
        self.v1 = None
        try:
            # Try to load in-cluster config (for Pod environment)
            config.load_incluster_config()
            self.v1 = client.CoreV1Api()
            logger.info("Loaded in-cluster Kubernetes config")
        except Exception as e:
            try:
                config.load_kube_config()
                self.v1 = client.CoreV1Api()
                logger.info("Loaded kubeconfig")
            except Exception as e2:
                logger.warning("Failed to load Kubernetes config: %s, %s", e, e2)

    def get_node_hostname_by_pod_ip(self, pod_ip: str) -> str | None:
        """Get Kubernetes node hostname (nodeName) by Pod IP"""
        if self.v1 is None:
            logger.warning("Kubernetes client not available, cannot get node hostname")
            return None

        try:
            # Find Pod by IP and return its nodeName
            pods = self.v1.list_pod_for_all_namespaces(field_selector=f"status.podIP={pod_ip}")
            for pod in pods.items:
                node_name = getattr(pod.spec, "node_name", None)
                if node_name:
                    return node_name
            logger.warning("Pod with IP %s not found in Kubernetes cluster", pod_ip)
            return None
        except Exception as e:
            logger.error("Error getting node hostname for Pod IP %s: %s", pod_ip, e)
            return None

    def list_pod_ips(
        self,
        namespace: str,
        label_selector: str | None = None,
        name_prefix: str | None = None,
    ) -> list[str]:
        """Return Running pod IPs matching a label or name prefix."""
        if self.v1 is None:
            logger.warning("Kubernetes client not available, cannot list pod IPs")
            return []
        if not namespace:
            logger.warning("Skip cluster-wide pod IP listing; namespace is empty")
            return []
        try:
            pods = self.v1.list_namespaced_pod(namespace, label_selector=label_selector or None)
            ips: list[str] = []
            for pod in pods.items:
                name = getattr(getattr(pod, "metadata", None), "name", "") or ""
                if name_prefix and not name.startswith(name_prefix):
                    continue
                status = getattr(pod, "status", None)
                phase = getattr(status, "phase", "")
                if phase and phase != "Running":
                    continue
                ip = getattr(status, "pod_ip", None) or getattr(status, "podIP", None)
                if ip:
                    ips.append(ip)
            return ips
        except Exception as e:
            logger.warning("Failed to list pod IPs namespace=%s: %s", namespace, e)
            return []

    def is_available(self) -> bool:
        """Check if Kubernetes client is available and initialized"""
        return self.v1 is not None
