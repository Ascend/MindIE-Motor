#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import sys
from pathlib import Path


def main():
    # 获取根目录, parents[1]表示获取当前文件的上两级目录。例如：脚本位于：usr/local/mindie_motor/coordinator/main.py，
    # parents[1] 会返回mindie_motor路径
    pkg_root = Path(__file__).resolve().parents[1]

    ms_coordinator_path = pkg_root / "bin" / "ms_coordinator"
    if not ms_coordinator_path.is_file():
        raise RuntimeError(f"mindie_motor_controller not found at {ms_coordinator_path}")

    env = os.environ.copy()
    lib_dir = pkg_root / "lib"
    if not lib_dir.is_dir():
        raise RuntimeError(f"lib directory not found at {lib_dir}")

    env["MIES_INSTALL_PATH"] = str(pkg_root)
    env["LD_LIBRARY_PATH"] = f"{lib_dir}:{lib_dir}/grpc:{env.get('LD_LIBRARY_PATH', '')}"

    # sys.argv[1:] 是用户传给 mindie_motor_controller 的参数
    os.execve(
        str(ms_coordinator_path),
        [str(ms_coordinator_path)] + sys.argv[1:],
        env
    )

if __name__ == "__main__":
    main()
