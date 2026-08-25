# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Store-process bootstraps run inside the store subprocess (``python -m``),
setting up what the store process needs before ``mooncake_store_service``
starts (an ACL context never survives the Popen boundary).

- ``ascend_850`` — 850: ACL context only, comm-free like 800I (pending A5 re-validation).
- ``ascend_800I`` — 800I: ACL context only, deliberately no comm (EI0014).
"""
