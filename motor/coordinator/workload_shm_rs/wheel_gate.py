# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Native members inside a packaged motor wheel.

Coordinator has no Python ledger fallback: a deployable wheel must contain the
workload-shm cdylib. kv-conductor is packed when ``build.sh`` produced the binary
(source-dev can still load ``target/release`` without packaging).

``build.sh`` also retags the pep517 ``py3-none-any`` artifact with the host
architecture so x86_64 and aarch64 wheels do not collide. Both the filename
and ``*.dist-info/WHEEL`` ``Tag:`` are rewritten so they stay consistent.
"""

import base64
import hashlib
import platform
import zipfile
from pathlib import Path

# Must match native.py _LIB_BASENAME and setup.py package_data.
WORKLOAD_SHM_WHEEL_MEMBER = "motor/coordinator/workload_shm_rs/lib/libmindie_workload_shm.so"
KV_CONDUCTOR_WHEEL_MEMBER = "motor/kv_conductor/bin/kv-conductor"

_REQUIRED_NATIVE_MEMBERS = (WORKLOAD_SHM_WHEEL_MEMBER,)
_MACHINE_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


def _archive_names(wheel_path: str) -> set[str]:
    with zipfile.ZipFile(wheel_path) as archive:
        return set(archive.namelist())


def list_missing_required_native_libs(wheel_path: str) -> list[str]:
    """Return required native archive members that ``wheel_path`` does not contain."""
    names = _archive_names(wheel_path)
    return [member for member in _REQUIRED_NATIVE_MEMBERS if member not in names]


def assert_motor_wheel_has_workload_shm(wheel_path: str) -> None:
    """Raise ValueError when the wheel is missing the workload-shm cdylib."""
    missing = list_missing_required_native_libs(wheel_path)
    if missing:
        raise ValueError(
            "refusing to emit motor wheel without required native library: "
            + ", ".join(missing)
            + ". Coordinator cannot start without libmindie_workload_shm.so "
            "(no Python ledger fallback). Build it via bash build.sh "
            "(cargo or WORKLOAD_SHM_PREBUILT) before pip wheel."
        )


def assert_motor_wheel_has_kv_conductor(wheel_path: str) -> None:
    """Raise ValueError when kv-conductor was built but not packed into the wheel."""
    if KV_CONDUCTOR_WHEEL_MEMBER not in _archive_names(wheel_path):
        raise ValueError(
            "refusing to emit motor wheel: kv-conductor binary was built but is "
            f"missing from the archive ({KV_CONDUCTOR_WHEEL_MEMBER})."
        )


def resolve_motor_wheel_platform_tag(*, machine: str | None = None) -> str:
    """Return the arch tag that replaces ``any`` on this host.

    Official artifacts are ``x86_64`` / ``aarch64``. Machine aliases
    (``amd64``, ``arm64``) are normalized so CI and ``uname -m`` agree.
    """
    mach = (machine if machine is not None else platform.machine()).strip().lower()
    return _MACHINE_ALIASES.get(mach, mach.replace("-", "_") or "unknown")


def arch_tagged_motor_wheel_name(version: str, *, platform_tag: str | None = None) -> str:
    """Keep the pep517 name and swap ``any`` for the host arch. No ``linux_`` prefix."""
    tag = platform_tag if platform_tag is not None else resolve_motor_wheel_platform_tag()
    return f"motor-{version}-py3-none-{tag}.whl"


def retag_motor_wheel_filename(
    wheel_path: str,
    version: str,
    *,
    platform_tag: str | None = None,
) -> str:
    """Rename the pep517 any-wheel and rewrite ``*.dist-info/WHEEL`` ``Tag:``.

    Filename and metadata both become ``py3-none-<arch>`` (``x86_64`` /
    ``aarch64``). ``RECORD`` is updated when that member exists. Returns the
    destination path. Filename is unchanged when already tagged; metadata is
    still rewritten so a stale ``py3-none-any`` Tag cannot linger.
    """
    src = Path(wheel_path)
    arch = platform_tag if platform_tag is not None else resolve_motor_wheel_platform_tag()
    dest = src.with_name(arch_tagged_motor_wheel_name(version, platform_tag=arch))
    if src.resolve() != dest.resolve():
        if dest.exists():
            dest.unlink()
        src.rename(dest)
    _rewrite_wheel_metadata_tag(dest, f"py3-none-{arch}")
    return str(dest)


def _record_sha256(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _replace_wheel_tag_lines(raw: bytes, metadata_tag: str) -> bytes:
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.startswith("Tag:"):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            out.append(f"Tag: {metadata_tag}{ending}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        raise ValueError("WHEEL metadata has no Tag: line")
    return "".join(out).encode("utf-8")


def _update_record_for_member(raw: bytes, member: str, data: bytes) -> bytes:
    digest = _record_sha256(data)
    size = str(len(data))
    lines = raw.decode("utf-8").splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        path = line.split(",", 1)[0]
        if path == member:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            out.append(f"{member},{digest},{size}{ending}")
        else:
            out.append(line)
    return "".join(out).encode("utf-8")


def _dist_info_member(names: list[str], suffix: str) -> str | None:
    matches = [name for name in names if name.endswith(f".dist-info/{suffix}")]
    if len(matches) > 1:
        raise ValueError("wheel has multiple *.dist-info/" + suffix + " members")
    return matches[0] if matches else None


def _rewrite_wheel_metadata_tag(wheel_path: Path, metadata_tag: str) -> None:
    """Rewrite ``Tag:`` in ``*.dist-info/WHEEL`` and the matching RECORD row."""
    tmp_path = wheel_path.with_suffix(wheel_path.suffix + ".retag-tmp")
    with zipfile.ZipFile(wheel_path, "r") as src:
        names = src.namelist()
        wheel_member = _dist_info_member(names, "WHEEL")
        if wheel_member is None:
            raise ValueError("wheel is missing *.dist-info/WHEEL")
        record_member = _dist_info_member(names, "RECORD")
        contents = {info.filename: src.read(info.filename) for info in src.infolist()}
        infos = list(src.infolist())

    contents[wheel_member] = _replace_wheel_tag_lines(contents[wheel_member], metadata_tag)
    if record_member is not None:
        contents[record_member] = _update_record_for_member(
            contents[record_member], wheel_member, contents[wheel_member]
        )

    with zipfile.ZipFile(tmp_path, "w") as dest:
        for info in infos:
            dest.writestr(info, contents[info.filename])
    tmp_path.replace(wheel_path)
