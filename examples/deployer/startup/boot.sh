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
    "kv_pool")
        source "$SCRIPT_DIR/kv_pool.sh"
        ;;
    "kv_conductor")
        source "$SCRIPT_DIR/kv_conductor.sh"
        ;;
    "mf_store")
        source "$SCRIPT_DIR/mf_store.sh"
        ;;
    *)
        echo "Error: Unknown ROLE=$ROLE"
        echo "Valid roles: SINGLE_CONTAINER, encode, prefill, decode, union, controller, coordinator, kv_pool, kv_conductor, mf_store"
        exit 1
        ;;
esac
