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
import types
from pathlib import Path


PATCH_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer" / "patch"
APPLIER_PATH = PATCH_ROOT / "patch_apply_agent_hint.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("patch_apply_agent_hint", APPLIER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


applier = _load_applier()


def test_base_version_strips_suffixes():
    assert applier._base_version("0.26.0") == "0.26.0"
    assert applier._base_version("0.26.0+git.abc123") == "0.26.0"
    assert applier._base_version("0.26.0-dev1") == "0.26.0"
    assert applier._base_version("0.26.0rc1") == "0.26.0rc1"


def test_normalize_strips_format_patch_envelope():
    raw = (
        "From 0123abcd Mon Sep 17 00:00:00 2026\n"
        "From: dev <dev@example.com>\n"
        "Subject: [PATCH] add agent hint\n"
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
    normalized = applier._normalize_patch(raw, strip_setup_py=False)
    assert normalized.startswith("diff --git ")
    assert "Subject:" not in normalized
    assert "From:" not in normalized
    assert "+content\n" in normalized


def test_normalize_strips_setup_py_hunk():
    raw = (
        "diff --git a/setup.py b/setup.py\n"
        "index 0000000..1111111 100644\n"
        "--- a/setup.py\n"
        "+++ b/setup.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/vllm_ascend/__init__.py b/vllm_ascend/__init__.py\n"
        "index 0000000..2222222 100644\n"
        "--- a/vllm_ascend/__init__.py\n"
        "+++ b/vllm_ascend/__init__.py\n"
        "@@ -1 +1 @@\n"
        "+def register_agent_hint(): pass\n"
    )
    normalized = applier._normalize_patch(raw, strip_setup_py=True)
    assert "setup.py" not in normalized
    assert "register_agent_hint" in normalized
    assert normalized.count("diff --git ") == 1


def test_should_apply_patch_version_gate(monkeypatch):
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0")
    assert applier.should_apply_patch() is True
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.25.0")
    assert applier.should_apply_patch() is False


def _fake_apply_patch(monkeypatch, result: bool) -> list[str]:
    """Record the packages whose patch was attempted, always returning ``result``."""
    calls: list[str] = []
    monkeypatch.setattr(applier, "apply_patch", lambda pkg_name, *args, **kwargs: calls.append(pkg_name) or result)
    return calls


def test_main_skips_all_patches_when_vllm_ascend_missing(monkeypatch):
    """Guards against leaving vLLM half-patched on a machine without vllm-ascend."""
    monkeypatch.setattr(applier.md, "version", lambda _name: "0.26.0")
    monkeypatch.setattr(applier, "vllm", types.SimpleNamespace(__path__=["/opt/vllm"]))
    monkeypatch.setattr(applier, "vllm_ascend", None)
    calls = _fake_apply_patch(monkeypatch, True)
    assert applier.main() == 0
    assert calls == []


def test_main_skips_all_patches_on_vllm_ascend_version_mismatch(monkeypatch):
    """Guards against half-patching vLLM when vllm-ascend upgraded past the target release."""
    versions = {"vllm": "0.26.0", "vllm_ascend": "0.26.0rc2"}
    monkeypatch.setattr(applier.md, "version", lambda name: versions[name])
    monkeypatch.setattr(applier, "vllm", types.SimpleNamespace(__path__=["/opt/vllm"]))
    monkeypatch.setattr(applier, "vllm_ascend", types.SimpleNamespace(__path__=["/opt/vllm_ascend"]))
    calls = _fake_apply_patch(monkeypatch, True)
    assert applier.main() == 0
    assert calls == []


def test_main_does_not_patch_vllm_ascend_when_vllm_patch_fails(monkeypatch):
    """Guards against registering an ascend entry point whose vLLM module is missing."""
    versions = {"vllm": "0.26.0", "vllm_ascend": "0.26.0rc1"}
    monkeypatch.setattr(applier.md, "version", lambda name: versions[name])
    monkeypatch.setattr(applier, "vllm", types.SimpleNamespace(__path__=["/opt/vllm"]))
    monkeypatch.setattr(applier, "vllm_ascend", types.SimpleNamespace(__path__=["/opt/vllm_ascend"]))
    monkeypatch.setattr(applier, "register_entry_point", lambda: True)
    calls = _fake_apply_patch(monkeypatch, False)
    assert applier.main() == 1
    assert calls == ["vllm"]


def test_main_applies_both_sides_and_registers_entry_point(monkeypatch):
    """Guards against the version gate over-skipping on a supported environment."""
    versions = {"vllm": "0.26.0", "vllm_ascend": "0.26.0rc1"}
    monkeypatch.setattr(applier.md, "version", lambda name: versions[name])
    monkeypatch.setattr(applier, "vllm", types.SimpleNamespace(__path__=["/opt/vllm"]))
    monkeypatch.setattr(applier, "vllm_ascend", types.SimpleNamespace(__path__=["/opt/vllm_ascend"]))
    registered: list[bool] = []
    monkeypatch.setattr(applier, "register_entry_point", lambda: registered.append(True) or True)
    calls = _fake_apply_patch(monkeypatch, True)
    assert applier.main() == 0
    assert calls == ["vllm", "vllm_ascend"]
    assert registered == [True]


def test_is_patched_validates_marker_and_syntax(tmp_path):
    target = tmp_path / "vllm_ascend" / "core" / "agent_hint"
    target.mkdir(parents=True)
    (target / "backend.py").write_text("ASCEND_AGENT_HINT_BACKEND = None\n", encoding="utf-8")
    rel_path = "vllm_ascend/core/agent_hint/backend.py"
    assert applier.is_patched(str(tmp_path), ((rel_path, "ASCEND_AGENT_HINT_BACKEND"),)) is True
    assert applier.is_patched(str(tmp_path), ((rel_path, "absent-marker"),)) is False
    assert applier.is_patched(str(tmp_path), (("vllm_ascend/missing.py", "ASCEND_AGENT_HINT_BACKEND"),)) is False


def test_is_patched_rejects_partially_applied_tree(tmp_path):
    """The agent_hint_manager.py module alone must not mark the vLLM patch as applied.

    A malformed hunk aborts ``patch`` after the new module is created but before
    the scheduler hooks land; treating that tree as patched shipped an image
    whose EngineCore died with AttributeError on the first request.
    """
    core = tmp_path / "vllm" / "v1" / "core"
    core.mkdir(parents=True)
    (core / "agent_hint_manager.py").write_text("def create_agent_hint_manager(): ...\n", encoding="utf-8")
    (core / "sched").mkdir()
    (core / "sched" / "scheduler.py").write_text("class Scheduler: pass\n", encoding="utf-8")
    assert applier.is_patched(str(tmp_path), applier.VLLM_MARKERS) is False
    assert applier.missing_markers(str(tmp_path), applier.VLLM_MARKERS) == [
        "vllm/v1/core/sched/scheduler.py",
        "vllm/v1/engine/input_processor.py",
        "vllm/v1/request.py",
    ]


def test_register_in_file_inserts_after_section_and_is_idempotent(tmp_path):
    ep = tmp_path / "entry_points.txt"
    ep.write_text("[vllm.general_plugins]\n", encoding="utf-8")
    assert applier._register_in_file(str(ep), applier.ENTRY_POINT_LINE) is True
    lines = ep.read_text(encoding="utf-8").splitlines()
    assert lines[0] == applier.ENTRY_POINT_SECTION
    assert lines[1] == applier.ENTRY_POINT_LINE
    # second call must not duplicate the entry
    assert applier._register_in_file(str(ep), applier.ENTRY_POINT_LINE) is True
    assert ep.read_text(encoding="utf-8").count(applier.ENTRY_POINT_LINE) == 1


def test_register_in_file_missing_section_fails(tmp_path):
    ep = tmp_path / "entry_points.txt"
    ep.write_text("[console_scripts]\nfoo = bar:baz\n", encoding="utf-8")
    assert applier._register_in_file(str(ep), applier.ENTRY_POINT_LINE) is False


def test_register_in_file_unreadable_file_fails(tmp_path):
    # a directory path triggers the OSError branch
    assert applier._register_in_file(str(tmp_path), applier.ENTRY_POINT_LINE) is False
