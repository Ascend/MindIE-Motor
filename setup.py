#!/usr/bin/env python
# coding=utf-8
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
import subprocess
import logging
import shutil
from pathlib import Path
from setuptools.command.build_py import build_py as _build_py
from setuptools import setup, find_packages
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

logging.basicConfig(level=logging.INFO)
os.environ["SOURCE_DATE_EPOCH"] = "315532800"  # 315532800 means 1980-01-01 00:00:00 UTC


def use_cxx11_abi() -> str:
    """
    Return whether to use CXX11 ABI as a string ("0" or "1").
    Priority:
    1. Environment variable GLOBAL_ABI_VERSION
    2. torch.compiled_with_cxx11_abi() (if torch is available)
    3. Fallback to "0" with warning
    """
    # 1. Environment override
    abi = os.getenv("GLOBAL_ABI_VERSION")
    if abi is not None:
        logging.info(f"Detect ABI from GLOBAL_ABI_VERSION, set GLOBAL_ABI_VERSION to {abi}")
        return abi

    # 2. Try to detect from torch
    try:
        import torch  # noqa: F401
        abi = torch.compiled_with_cxx11_abi()
        if abi is not None:
            abi = str(int(bool(abi)))
            logging.info(f"Detect ABI from torch, set GLOBAL_ABI_VERSION to {abi}")
            return abi
    except Exception:
        logging.warning(f"No torch detected on current environment.")

    # 3. Fallback to "0" with warning
    abi = "0"
    logging.warning(
        f"GLOBAL_ABI_VERSION is not set and failed to detect ABI from torch, set USE_CXX11_ABI to {abi}.",
    )
    return abi


class CustomBuildPy(_build_py):
    def run(self):
        logging.info(">>> Running build.sh to compile shared libraries...")

        project_root = Path(__file__).resolve().parent
        build_dir = project_root / "build"
        subprocess.run(
            ["bash", "build.sh", "-b", "ms"],
            cwd=str(build_dir),
            check=True,
            shell=False
        )

        build_pkg = Path(self.build_lib) / "mindie_motor"
        config_str = "conf"
        examples_str = "examples"
        scripts_str = "kubernetes_deploy_scripts"

        (build_pkg / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copytree("install/bin", build_pkg / "bin", dirs_exist_ok=True)
        (build_pkg / config_str).mkdir(parents=True, exist_ok=True)
        shutil.copytree("install/config", build_pkg / config_str, dirs_exist_ok=True)
        (build_pkg / examples_str / scripts_str).mkdir(parents=True, exist_ok=True)
        shutil.copytree("mindie_service/management_service/example/deploy_scripts",
            build_pkg / examples_str / scripts_str, dirs_exist_ok=True)
        (build_pkg / "scripts" / "http_client_ctl").mkdir(parents=True, exist_ok=True)
        shutil.copytree("mindie_service/utils/http_client_ctl/scripts",
            build_pkg / "scripts" / "http_client_ctl", dirs_exist_ok=True)
        shutil.copy("mindie_service/management_service/config/node_manager.json",
            build_pkg / examples_str / scripts_str / config_str)
        self.copy_third_party()

        super().run()

    def copy_third_party(self):
        grpc_str = "grpc"
        project_root = Path(__file__).resolve().parent
        build_pkg = Path(self.build_lib) / "mindie_motor"
        lib_dir = build_pkg / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        (lib_dir / grpc_str).mkdir(parents=True, exist_ok=True)

        # 定义源目录与目标目录的映射关系
        lib_mappings = {
            "boost": lib_dir,
            grpc_str: lib_dir / grpc_str,
            "libboundscheck": lib_dir,
            "openssl": lib_dir,
            "prometheus-cpp": lib_dir,
            "spdlog": lib_dir
        }

        # 不需要拷贝的文件
        exclude_extensions = {'.pc', '.cmake', '.json'}

        def copy_dir_filtered(src_dir: Path, dst_dir: Path):
            if not src_dir.exists():
                logging.warning(f"No such directory when copy it: {src_dir}")
                return

            dst_dir.mkdir(parents=True, exist_ok=True)
            for item in src_dir.iterdir():
                if item.is_file() and item.suffix in exclude_extensions:
                    continue
                dst_path = dst_dir / item.name
                if item.is_file():
                    shutil.copy2(item, dst_path)
                elif item.is_dir():
                    copy_dir_filtered(item, dst_path)

        for lib_name, target_dir in lib_mappings.items():
            src_path = project_root / "third_party" / "install" / lib_name / "lib"
            copy_dir_filtered(src_path, target_dir)


class BDistWheel(_bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        # 标记为二进制 wheel，否则会生成 py3-none-any
        self.root_is_pure = False


def get_version() -> str:
    """
    Return version string.

    Priority:
    1. Environment variable MINDIE_MOTOR_VERSION_OVERRIDE
    2. Default version
    """
    version = os.getenv("MINDIE_MOTOR_VERSION_OVERRIDE", "1.0.0")
    logging.info(f"Use mindie llm version: {version}")
    return version


setup(
    name="mindie-motor",
    version=get_version(),
    author="ascend",
    author_email="",
    description="MindIE Motor Project",
    long_description="",
    install_requires=[],
    zip_safe=False,
    python_requires=">=3.10",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "mindie_motor_coordinator = mindie_motor.coordinator.main:main",
            "mindie_motor_controller = mindie_motor.controller.main:main",
        ]
    },
    cmdclass={
        "build_py": CustomBuildPy,
        "bdist_wheel": BDistWheel
    }
)
