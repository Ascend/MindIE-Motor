# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Apply the vLLM and vLLM-Ascend patches required by container snapshots."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SNAPSHOT_CONFIG_KEY = "motor_container_snapshot_config"
ENABLE_SNAPSHOT_KEY = "enable_snapshot"
PATCH_VERSION = "0.30.0"


def _snapshot_enabled(config_path: Path) -> bool:
    with config_path.open(encoding="utf-8") as config_file:
        user_config = json.load(config_file)
    snapshot_config = user_config.get(SNAPSHOT_CONFIG_KEY)
    if snapshot_config is None:
        return False
    if not isinstance(snapshot_config, dict):
        raise ValueError(f"'{SNAPSHOT_CONFIG_KEY}' must be an object")
    enabled = snapshot_config.get(ENABLE_SNAPSHOT_KEY, False)
    if not isinstance(enabled, bool):
        raise ValueError(f"'{SNAPSHOT_CONFIG_KEY}.{ENABLE_SNAPSHOT_KEY}' must be a JSON boolean")
    return enabled


def _snapshot_enabled_from_environment() -> bool:
    config_value = os.environ.get("USER_CONFIG_PATH")
    if not config_value:
        return False
    config_path = Path(config_value)
    if not config_path.is_file():
        return False
    return _snapshot_enabled(config_path)


def check_enabled() -> int:
    """Return 0 when snapshots are enabled, 1 when disabled, and 2 for invalid config."""
    try:
        return 0 if _snapshot_enabled_from_environment() else 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logger.error("Failed to read container snapshot configuration: %s", error)
        return 2


def main() -> int:
    try:
        enabled = _snapshot_enabled_from_environment()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logger.error("Failed to read container snapshot configuration: %s", error)
        return 1
    if not enabled:
        logger.info("Skip container snapshot patches: container snapshot is disabled")
        return 0

    patch_root = Path(__file__).resolve().parent / PATCH_VERSION
    apply_script = patch_root / "apply_patches.sh"
    patches = (
        patch_root / "container_snapshot" / "vllm_v0.30.0_container_snapshot.patch",
        patch_root / "container_snapshot" / "vllm_ascend_v0.30.0_container_snapshot.patch",
    )
    required_files = (apply_script, *patches)
    missing_files = [str(path) for path in required_files if not path.is_file()]
    if missing_files:
        logger.error("Container snapshot patch files are missing: %s", ", ".join(missing_files))
        return 1

    logger.info("Container snapshot is enabled; applying vLLM and vLLM-Ascend patches")
    result = subprocess.run(["/bin/bash", str(apply_script), *map(str, patches)], check=False)
    if result.returncode != 0:
        logger.error("Failed to apply container snapshot patches: rc=%s", result.returncode)
    return result.returncode


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-enabled"]:
        sys.exit(check_enabled())
    if len(sys.argv) != 1:
        logger.error("Usage: %s [--check-enabled]", sys.argv[0])
        sys.exit(2)
    sys.exit(main())
