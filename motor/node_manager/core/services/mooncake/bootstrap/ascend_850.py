# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""850 (350/850/950) store bootstrap: ACL context only, comm-free like 800I.

The HCCL comm mirror was dropped pending A5 re-validation. The store never
calls ``aclrtSetDevice`` itself, so the device context is still required.
"""

import logging
import runpy
import sys

logger = logging.getLogger("motor.mooncake.store_bootstrap")


def _init_logging() -> None:
    """Ensure the store subprocess logs to stderr.

    The store runs as a bare subprocess (see mooncake/lifecycle.py) without the
    motor logging setup, so configure a minimal stderr handler up front.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[mooncake store bootstrap] %(asctime)s %(levelname)s %(message)s",
    )


def _init_acl_context() -> None:
    """Create a device-0 ACL context for the store process (best effort)."""
    try:
        import acl

        acl.init()
        acl.rt.set_device(0)
    except Exception as e:  # pylint: disable=broad-except
        # Non-fatal by design: the store must still start and serve TCP-only clients.
        logger.warning("ACL context init failed (non-fatal): %s", e)


def main() -> None:
    """Bootstrap the device context, then run the official store service
    (``--config``/``--port`` in ``sys.argv`` are consumed by the store itself).
    """
    _init_logging()
    _init_acl_context()
    runpy.run_module("mooncake.mooncake_store_service", run_name="__main__")


if __name__ == "__main__":
    main()
