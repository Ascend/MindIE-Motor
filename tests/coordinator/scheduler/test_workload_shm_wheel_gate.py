# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Guards the motor-wheel contract: deployable wheels must ship native members."""

import base64
import hashlib
import zipfile
from pathlib import Path

import pytest

from motor.coordinator.scheduler.runtime.workload_shm import native
from motor.coordinator.workload_shm_rs.wheel_gate import (
    KV_CONDUCTOR_WHEEL_MEMBER,
    WORKLOAD_SHM_WHEEL_MEMBER,
    arch_tagged_motor_wheel_name,
    assert_motor_wheel_has_kv_conductor,
    assert_motor_wheel_has_workload_shm,
    list_missing_required_native_libs,
    resolve_motor_wheel_platform_tag,
    retag_motor_wheel_filename,
)

_WHEEL_MEMBER = "motor-3.1.0.dist-info/WHEEL"
_RECORD_MEMBER = "motor-3.1.0.dist-info/RECORD"


def _record_sha256(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _write_pep517_wheel(path: Path, *, tag: str = "py3-none-any") -> None:
    """Write a minimal pep517-shaped wheel so retag can rewrite WHEEL + RECORD."""
    wheel_body = (f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n").encode()
    record = f"{_WHEEL_MEMBER},{_record_sha256(wheel_body)},{len(wheel_body)}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(_WHEEL_MEMBER, wheel_body)
        archive.writestr(_RECORD_MEMBER, record)
        archive.writestr("motor/__init__.py", b"")


def _wheel_tags(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        text = archive.read(_WHEEL_MEMBER).decode()
    return [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("Tag:")]


def _record_row(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        for line in archive.read(_RECORD_MEMBER).decode().splitlines():
            if line.split(",", 1)[0] == member:
                return line
    raise AssertionError(f"RECORD is missing {member}")


def test_wheel_member_matches_runtime_loader_basename():
    """Packaged path must be the same basename native.py searches under lib/."""
    assert WORKLOAD_SHM_WHEEL_MEMBER.endswith("/lib/" + native._LIB_BASENAME)


def test_list_missing_required_native_libs_reports_absent_so(tmp_path: Path):
    """A wheel without the cdylib must be rejected so nightly cannot ship an empty ledger."""
    wheel = tmp_path / "motor-empty-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("motor/__init__.py", "")

    assert list_missing_required_native_libs(str(wheel)) == [WORKLOAD_SHM_WHEEL_MEMBER]
    with pytest.raises(ValueError, match="refusing to emit motor wheel"):
        assert_motor_wheel_has_workload_shm(str(wheel))


def test_list_missing_required_native_libs_accepts_packaged_so(tmp_path: Path):
    """Presence of the packaged member is enough; the gate does not inspect ELF contents."""
    wheel = tmp_path / "motor-ok-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(WORKLOAD_SHM_WHEEL_MEMBER, b"\x7fELF")

    assert list_missing_required_native_libs(str(wheel)) == []
    assert_motor_wheel_has_workload_shm(str(wheel))


def test_assert_motor_wheel_has_kv_conductor_rejects_missing_bin(tmp_path: Path):
    """When cargo produced kv-conductor, build.sh must not keep a wheel without the binary."""
    wheel = tmp_path / "motor-no-kv-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(WORKLOAD_SHM_WHEEL_MEMBER, b"\x7fELF")

    with pytest.raises(ValueError, match="refusing to emit motor wheel"):
        assert_motor_wheel_has_kv_conductor(str(wheel))


def test_assert_motor_wheel_has_kv_conductor_accepts_packaged_bin(tmp_path: Path):
    """Presence of the packaged member is enough; the gate does not inspect the ELF."""
    wheel = tmp_path / "motor-with-kv-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(WORKLOAD_SHM_WHEEL_MEMBER, b"\x7fELF")
        archive.writestr(KV_CONDUCTOR_WHEEL_MEMBER, b"\x7fELF")

    assert_motor_wheel_has_kv_conductor(str(wheel))


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "linux_x86_64"),
        ("amd64", "linux_x86_64"),
        ("aarch64", "linux_aarch64"),
        ("arm64", "linux_aarch64"),
    ],
)
def test_resolve_motor_wheel_platform_tag_normalizes_host(machine: str, expected: str):
    """Linux pip only accepts PEP 425 tags like linux_aarch64, not a bare aarch64."""
    assert resolve_motor_wheel_platform_tag(machine=machine) == expected


def test_arch_tagged_motor_wheel_name_keeps_pep517_prefix():
    """Filename stays motor-<ver>-py3-none-linux_<arch>.whl."""
    assert arch_tagged_motor_wheel_name("3.1.0", platform_tag="linux_x86_64") == (
        "motor-3.1.0-py3-none-linux_x86_64.whl"
    )
    assert arch_tagged_motor_wheel_name("3.1.0", platform_tag="linux_aarch64") == (
        "motor-3.1.0-py3-none-linux_aarch64.whl"
    )


def test_retag_motor_wheel_filename_replaces_any_tag(tmp_path: Path):
    """Filename and WHEEL Tag must both become linux_<arch> so pip can install."""
    src = tmp_path / "motor-3.1.0-py3-none-any.whl"
    _write_pep517_wheel(src)

    dest = Path(retag_motor_wheel_filename(str(src), "3.1.0", platform_tag="linux_x86_64"))

    assert dest == tmp_path / "motor-3.1.0-py3-none-linux_x86_64.whl"
    assert dest.is_file()
    assert not src.exists()
    assert _wheel_tags(dest) == ["py3-none-linux_x86_64"]
    with zipfile.ZipFile(dest) as archive:
        new_wheel = archive.read(_WHEEL_MEMBER)
    assert _record_row(dest, _WHEEL_MEMBER) == (f"{_WHEEL_MEMBER},{_record_sha256(new_wheel)},{len(new_wheel)}")


def test_retag_motor_wheel_filename_is_noop_when_already_tagged(tmp_path: Path):
    """Re-running the gate on an already tagged wheel must not invent a second file."""
    src = tmp_path / "motor-3.1.0-py3-none-linux_aarch64.whl"
    _write_pep517_wheel(src, tag="py3-none-any")

    dest = Path(retag_motor_wheel_filename(str(src), "3.1.0", platform_tag="linux_aarch64"))

    assert dest == src
    assert src.is_file()
    assert _wheel_tags(dest) == ["py3-none-linux_aarch64"]
