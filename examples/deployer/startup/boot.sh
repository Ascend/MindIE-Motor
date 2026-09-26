#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
# Host-network IPv6 engines cannot use ClusterIP DNS. The file is only shipped
# when the A5 host-nic overlay is on, so IPv4 pod-network boots do not see it.
# Deploy rewrites the ConfigMap after Endpoints appear; kubelet refreshes the
# mount after the process has started, so wait until the file has lines.
if [ -e "$SCRIPT_DIR/service-hosts" ]; then
    _hosts_wait=0
    while [ ! -s "$SCRIPT_DIR/service-hosts" ] && [ "$_hosts_wait" -lt 90 ]; do
        sleep 2
        _hosts_wait=$((_hosts_wait + 1))
    done
    if [ -s "$SCRIPT_DIR/service-hosts" ]; then
        cat "$SCRIPT_DIR/service-hosts" >> /etc/hosts
    else
        echo "Warning: service-hosts is empty; hostNetwork name resolution may fail"
    fi
fi

# Advertise the node EID as POD_IP when IPV6_NIC is set. MOTOR_ENGINE_IPV4=1
# keeps the pod-network address for an explicit IPv4 engine.
if [ "$ROLE" = "prefill" ] || [ "$ROLE" = "decode" ]; then
    if [ "${MOTOR_ENGINE_IPV4:-}" != "1" ] && [ -z "${MOTOR_ADVERTISE_IP:-}" ] && [ -n "${IPV6_NIC:-}" ]; then
        MOTOR_ADVERTISE_IP=$(ip -6 -o addr show dev "$IPV6_NIC" scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
        if [ -n "${MOTOR_ADVERTISE_IP:-}" ]; then
            export POD_IP="$MOTOR_ADVERTISE_IP"
        fi
    fi
fi

case "$ROLE" in
    "SINGLE_CONTAINER")
        source "$SCRIPT_DIR/all_combine_in_single_container.sh"
        ;;
    "encode"|"prefill"|"decode"|"union")
        source "$SCRIPT_DIR/engine.sh"
        ;;
    "controller")
        source "$SCRIPT_DIR/controller.sh"
        ;;
    "coordinator")
        source "$SCRIPT_DIR/coordinator.sh"
        ;;
    "coordinator_controller")
        source "$SCRIPT_DIR/coordinator_controller.sh"
        ;;
    "kv_store")
        source "$SCRIPT_DIR/kv_cache_store.sh"
        ;;
    "render")
        source "$SCRIPT_DIR/render.sh"
        ;;
    "kv_conductor")
        source "$SCRIPT_DIR/kv_conductor.sh"
        ;;
    "mf_store")
        source "$SCRIPT_DIR/mf_store.sh"
        ;;
    *)
        echo "Error: Unknown ROLE=$ROLE"
        echo "Valid roles: SINGLE_CONTAINER, encode, prefill, decode, union, controller, coordinator, coordinator_controller, kv_store, kv_conductor, mf_store, render"
        exit 1
        ;;
esac
