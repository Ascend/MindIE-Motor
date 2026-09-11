# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared preparation helpers for Docker, K8s, and Slurm deployers."""

from __future__ import annotations

from collections.abc import Callable
import importlib.util
from pathlib import Path
import shutil


_STATIC_ASSETS = (
    ("startup/boot.sh", "boot.sh"),
    ("startup/common.sh", "common.sh"),
    ("startup/hccl_tools.py", "hccl_tools.py"),
    ("probe/probe.sh", "probe.sh"),
    ("probe/probe.py", "probe.py"),
    ("prestop/prestop.sh", "prestop.sh"),
    ("prestop/prestop.py", "prestop.py"),
    ("startup/roles/kv_store_backends/mooncake/mooncake.sh", "kv_store_backends.mooncake.mooncake.sh"),
    ("startup/roles/kv_store_backends/mooncake/mooncake_config.py", "mooncake_config.py"),
    ("startup/roles/kv_store_backends/memcache/memcache.sh", "kv_store_backends.memcache.memcache.sh"),
    (
        "startup/roles/kv_store_backends/memcache/memcache_meta_service.py",
        "kv_store_backends.memcache.memcache_meta_service.py",
    ),
    (
        "startup/roles/kv_store_backends/memcache/mmc-local-inprocess.conf",
        "kv_store_backends.memcache.mmc-local-inprocess.conf",
    ),
    (
        "startup/roles/kv_store_backends/memcache/mmc-local-standalone.conf",
        "kv_store_backends.memcache.mmc-local-standalone.conf",
    ),
)


def configmap_assets(deployer_dir: str | Path) -> list[tuple[Path, str]]:
    """Return canonical deployer assets with their names inside a ConfigMap."""
    deployer_path = Path(deployer_dir)
    assets = [(deployer_path / source, target) for source, target in _STATIC_ASSETS]
    roles_dir = deployer_path / "startup" / "roles"
    assets.extend((source, source.name) for source in sorted(roles_dir.glob("*.sh")))
    return assets


def prepare_local_configmap(
    deployer_dir: str | Path,
    configmap_path: str | Path,
    user_config_path: str | Path,
    env_config_path: str | Path,
) -> None:
    """Copy shared assets and the two resolved JSON inputs into a local directory."""
    destination = Path(configmap_path)
    destination.mkdir(parents=True, exist_ok=True)
    for source, target_name in configmap_assets(deployer_dir):
        shutil.copy2(source, destination / target_name)
    shutil.copy2(user_config_path, destination / "user_config.json")
    shutil.copy2(env_config_path, destination / "env.json")


def prepare_rendered_local_configmap(
    deployer_dir: str | Path,
    configmap_path: str | Path,
    user_config_path: str | Path,
    env_config_path: str | Path,
    *,
    before_render: Callable[[Path], None] | None = None,
) -> None:
    """Copy a local ConfigMap and render its shell environment exactly once.

    ``before_render`` lets a deployer adjust the copied ``user_config.json``
    without modifying the caller's source configuration.
    """
    destination = Path(configmap_path)
    prepare_local_configmap(deployer_dir, destination, user_config_path, env_config_path)
    if before_render is not None:
        before_render(destination / "user_config.json")

    module_path = Path(deployer_dir) / "startup" / "set_env_docker.py"
    spec = importlib.util.spec_from_file_location("set_env_docker", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load ConfigMap environment renderer: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.set_env_docker(str(destination))


def kubectl_from_file_args(deployer_dir: str | Path) -> list[str]:
    """Build explicit kubectl ``--from-file=name=path`` arguments for shared assets."""
    return [f"--from-file={target_name}={source}" for source, target_name in configmap_assets(deployer_dir)]
