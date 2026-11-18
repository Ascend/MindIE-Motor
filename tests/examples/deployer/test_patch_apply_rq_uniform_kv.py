# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


import importlib.util
import os
import types
from pathlib import Path


PATCH_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer" / "patch"
APPLIER_PATH = PATCH_ROOT / "patch_apply_rq_uniform_kv.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("patch_apply_rq_uniform_kv", APPLIER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


applier = _load_applier()


def _expected_patch_path() -> str:
    script_dir = os.path.dirname(os.path.abspath(applier.__file__))
    return os.path.join(script_dir, applier.PATCH_RELPATH)


def test_base_version_strips_suffixes():
    assert applier._base_version("0.26.0") == "0.26.0"
    assert applier._base_version("0.26.0+git.abc123") == "0.26.0"
    assert applier._base_version("0.26.0-dev1") == "0.26.0"
    assert applier._base_version("0.26.0rc1") == "0.26.0rc1"


def test_normalize_strips_format_patch_envelope():
    raw = (
        "From 0123abcd Mon Sep 17 00:00:00 2026\n"
        "From: dev <dev@example.com>\n"
        "Subject: [PATCH] add rq_uniform kv\n"
        "\n"
        "---\n"
        " a/file.py | 1 +\n"
        " 1 file changed, 1 insertion(+)\n"
        "\n"
        "diff --git a/file.py b/file.py\n"
        "index 0000000..1111111 100644\n"
        "--- a/file.py\n"
        "+++ b/file.py\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
    )
    normalized = applier._normalize_patch(raw)
    assert normalized.startswith("diff --git ")
    assert "Subject:" not in normalized
    assert "From:" not in normalized
    assert "+content\n" in normalized


def test_should_apply_patch_version_gate(monkeypatch):
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0")
    assert applier.should_apply_patch() is True
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0+git.abc")
    assert applier.should_apply_patch() is True
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.25.0")
    assert applier.should_apply_patch() is False

    def _missing(_name):
        raise applier.md.PackageNotFoundError

    monkeypatch.setattr(applier.md, "version", _missing)
    assert applier.should_apply_patch() is False


def test_ascend_patch_supported_version_gate(monkeypatch):
    """Guards against patching an Ascend tree that is missing or past the target release."""
    monkeypatch.setattr(applier, "vllm_ascend", None)
    assert applier.ascend_patch_supported() is False

    monkeypatch.setattr(applier, "vllm_ascend", types.SimpleNamespace(__path__=["/opt/vllm_ascend"]))
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0rc1")
    assert applier.ascend_patch_supported() is True
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0rc1+git.abc")
    assert applier.ascend_patch_supported() is True
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0rc2")
    assert applier.ascend_patch_supported() is False

    def _missing(_name):
        raise applier.md.PackageNotFoundError

    monkeypatch.setattr(applier.md, "version", _missing)
    assert applier.ascend_patch_supported() is False


def _fake_apply_patch(monkeypatch, result: bool) -> list[tuple[str, str]]:
    """Record each (root, patch_path) attempt and always return ``result``."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        applier,
        "apply_patch",
        lambda root, patch_path: calls.append((root, patch_path)) or result,
    )
    return calls


def _install_supported_env(monkeypatch):
    versions = {"vllm": "0.26.0", "vllm_ascend": "0.26.0rc1"}
    monkeypatch.setattr(applier.md, "version", lambda name: versions[name])
    monkeypatch.setattr(applier, "vllm_ascend", types.SimpleNamespace(__path__=["/opt/vllm_ascend"]))


def test_main_skips_when_vllm_ascend_missing(monkeypatch):
    """Guards against applying the patch on a machine without vllm-ascend."""
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0")
    monkeypatch.setattr(applier, "vllm_ascend", None)
    calls = _fake_apply_patch(monkeypatch, True)
    assert applier.main() == 0
    assert calls == []


def test_main_skips_on_vllm_ascend_version_mismatch(monkeypatch):
    """Guards against patching vllm-ascend after it moved past the target release."""
    versions = {"vllm": "0.26.0", "vllm_ascend": "0.26.0rc2"}
    monkeypatch.setattr(applier.md, "version", lambda name: versions[name])
    monkeypatch.setattr(applier, "vllm_ascend", types.SimpleNamespace(__path__=["/opt/vllm_ascend"]))
    calls = _fake_apply_patch(monkeypatch, True)
    assert applier.main() == 0
    assert calls == []


def test_main_returns_failure_when_apply_fails(monkeypatch):
    """Guards against reporting success when the ascend tree is still unpatched."""
    _install_supported_env(monkeypatch)
    calls = _fake_apply_patch(monkeypatch, False)
    assert applier.main() == 1
    assert calls == [("/opt", _expected_patch_path())]


def test_main_applies_versioned_patch_on_supported_env(monkeypatch):
    """Guards against the version gate over-skipping, and against a hardcoded patch directory."""
    _install_supported_env(monkeypatch)
    calls = _fake_apply_patch(monkeypatch, True)
    assert applier.main() == 0
    assert applier.PATCH_RELPATH == os.path.join(applier.VLLM_TARGET_VERSION, "vllm_ascend_rq_uniform_kv.patch")
    assert calls == [("/opt", _expected_patch_path())]


def test_is_patched_validates_marker_and_syntax(tmp_path):
    target = tmp_path / "vllm_ascend"
    target.mkdir()
    rel_path = "vllm_ascend/hybrid_kv_page.py"
    (target / "hybrid_kv_page.py").write_text("class HybridK8v4Page: pass\n", encoding="utf-8")
    assert applier.is_patched(str(tmp_path), ((rel_path, "class HybridK8v4Page"),)) is True
    assert applier.is_patched(str(tmp_path), ((rel_path, "absent-marker"),)) is False
    assert applier.is_patched(str(tmp_path), (("vllm_ascend/missing.py", "class HybridK8v4Page"),)) is False
    (target / "hybrid_kv_page.py").write_text("class HybridK8v4Page: (\n", encoding="utf-8")
    assert applier.is_patched(str(tmp_path), ((rel_path, "class HybridK8v4Page"),)) is False


def test_is_patched_rejects_partially_applied_tree(tmp_path):
    """The dtype helper alone must not mark the rq_uniform patch as applied.

    ``patch`` can stop after the first new module and leave the shared-file
    hooks unpatched; treating that tree as applied would skip the retry.
    """
    dtype = tmp_path / "vllm_ascend" / "patch" / "platform"
    dtype.mkdir(parents=True)
    (dtype / "patch_rq_uniform_cache_dtype.py").write_text(
        "def is_ascend_rq_uniform_kv_dtype(): ...\n",
        encoding="utf-8",
    )
    assert applier.is_patched(str(tmp_path), applier.MARKERS) is False
    assert applier.missing_markers(str(tmp_path), applier.MARKERS) == [
        "vllm_ascend/quantization/methods/kv_cache/rq_uniform/latent.py",
        "vllm_ascend/ascend_config.py",
        "vllm_ascend/hybrid_kv_page.py",
    ]
