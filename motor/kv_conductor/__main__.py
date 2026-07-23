# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Entry point for ``python -m motor.kv_conductor``.

Forwards all arguments to the kv-conductor binary via :func:`os.execvp`,
replacing the Python process.
"""

import os
import sys

from motor.kv_conductor import get_binary_path


def main() -> None:
    """Forward CLI arguments to the kv-conductor binary."""
    binary = get_binary_path()
    if binary is None:
        print(
            "ERROR: kv-conductor binary not found. Rebuild with: cd motor/kv_conductor && cargo build --release",
            file=sys.stderr,
        )
        sys.exit(1)
    # execvp replaces the Python process with the conductor binary
    os.execvp(str(binary), [str(binary)] + sys.argv[1:])


if __name__ == "__main__":
    main()
