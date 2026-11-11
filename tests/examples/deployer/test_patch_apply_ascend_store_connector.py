# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import ast
import shutil
import sys
from pathlib import Path

import pytest

MOTOR_ROOT = Path(__file__).resolve().parents[3]
PATCH_DIR = MOTOR_ROOT / "examples" / "deployer" / "patch"
sys.path.insert(0, str(PATCH_DIR))

from patch_apply import APPLIERS  # noqa: E402
from patch_apply_ascend_store_connector import (  # noqa: E402
    CONNECTOR_SPEC_V032,
    CPU_EVENT_SPEC_V030,
    CPU_EVENT_SPEC_V032,
    METADATA_SPEC_V032,
    PATCH_SPECS,
    TARGET_VLLM_VERSIONS,
    apply_patch,
    is_patched,
    patch_specs_for_version,
)

MARKER = "# motor-patch: ascend-store unique kv events (do not remove)"
CONNECTOR_REL = Path("vllm-ascend/vllm_ascend") / PATCH_SPECS[0][0]


def test_applier_is_registered():
    assert (
        "patch_apply_ascend_store_connector.py",
        "AscendStoreConnector unique-union KV events",
    ) in APPLIERS


def test_patch_applies_unique_union_and_is_idempotent(tmp_path):
    src = MOTOR_ROOT.parent / CONNECTOR_REL
    if not src.is_file():
        pytest.skip(f"upstream connector not in workspace: {src}")

    target = tmp_path / "ascend_store_connector.py"
    shutil.copyfile(src, target)
    patch_file = PATCH_DIR / PATCH_SPECS[0][1]
    assert patch_file.is_file()

    assert apply_patch(str(target), str(patch_file), MARKER)
    text = target.read_text(encoding="utf-8")
    ast.parse(text)
    assert MARKER in text
    assert "unique_events = list(dict.fromkeys(self._aggregator.get_all_events()))" in text
    assert "common_events = self._aggregator.get_common_events()" not in text
    assert is_patched(str(target), MARKER)

    assert apply_patch(str(target), str(patch_file), MARKER)


def test_cpu_event_grain_patch_applies_and_is_idempotent(tmp_path):
    rel_path, patch_name, marker = PATCH_SPECS[1]
    repo = MOTOR_ROOT.parent / "vllm-ascend"
    src = repo / "vllm_ascend" / rel_path
    if not src.is_file():
        pytest.skip(f"upstream kv_transfer not in workspace: {src}")

    import subprocess

    original = subprocess.check_output(
        ["git", "-C", str(repo), "show", f"HEAD:vllm_ascend/{rel_path}"],
        text=True,
    )
    target = tmp_path / "kv_transfer.py"
    target.write_text(original)
    patch_file = PATCH_DIR / patch_name
    assert patch_file.is_file()

    assert apply_patch(str(target), str(patch_file), marker)
    text = target.read_text(encoding="utf-8")
    ast.parse(text)
    assert marker in text
    assert "def conductor_cpu_event_block_size(" not in text
    assert "compress_ratio > 1" in text
    assert "compress_ratio in (4, 128)" not in text
    assert "effective_block_size if compress_ratio > 1 else original_block_size" in text
    assert "infer_cache_family_ratio(cache_family)" in text
    assert "start * compress_ratio, end * compress_ratio" in text
    assert "def _target_effective_block_size(" not in text
    assert is_patched(str(target), marker)
    assert apply_patch(str(target), str(patch_file), marker)


@pytest.mark.parametrize("patch_status", [0, 1])
def test_engine_applies_patches_before_starting_node_manager(patch_status):
    """A failed patch must prevent engine startup; a successful patch precedes it."""
    import subprocess

    script = MOTOR_ROOT / "examples/deployer/startup/roles/engine.sh"
    helpers = (
        "setup_jemalloc gen_ranktable_config set_cann_env set_mf_store_env "
        "setup_motor_log_path setup_ascend_work_path setup_ascend_cache_path set_prefill_env apply_instance_port_env"
    )
    stub = "\n".join(f"{name}() {{ :; }}" for name in helpers.split())
    stub += '\nis_a5_hardware() { return 1; }\nROLE=prefill\n'
    stub += f'apply_patches() {{ echo PATCH; return {patch_status}; }}\n'
    stub += 'python3() { echo ENGINE; }\n'
    result = subprocess.run(["bash"], input=stub + script.read_text(), text=True, capture_output=True, check=False)
    steps = [line for line in result.stdout.splitlines() if line in {"PATCH", "ENGINE"}]
    assert steps == (["PATCH", "ENGINE"] if patch_status == 0 else ["PATCH"])
    assert result.returncode == patch_status


@pytest.fixture(params=["c1", "c2", "c4", "c128"])
def cpu_sender(request):
    """Execute the patched sending path with a fake store, without an installed NPU engine."""
    import logging
    from types import SimpleNamespace
    from unittest.mock import Mock

    patch_text = (PATCH_DIR / PATCH_SPECS[1][1]).read_text()
    postimage = "\n".join(
        line[1:] for line in patch_text.splitlines() if line.startswith((" ", "+")) and not line.startswith("+++")
    )
    body = postimage[postimage.index("                continue") : postimage.index("class KVCacheStoreRecvingThread(")]
    # The hunk starts after upstream has applied masks and TP key sharding.
    preamble = '''class Sender:
    def send(self, req_meta, starts, ends):
        for group_id in [0]:
            token_len = len(req_meta.token_ids)
            req_id = "test"
            group_block_size = raw_group_block_size = 128
            cache_family = self.cache_family
            keys = [str(start) for start in starts]
            block_hashes = key_block_ids = block_ids = list(starts)
            current_event = None
            if not keys:
'''
    namespace = {
        "ReqMeta": SimpleNamespace,
        "BlockStored": SimpleNamespace,
        "logger": logging.getLogger(__name__),
        "get_block_hashes": lambda hashes, *_: hashes,
        "maybe_convert_block_hash": lambda value: value,
        "infer_cache_family_ratio": lambda family: int(family[1:]),
    }
    # Execute only the checked-in patch, with engine dependencies replaced above.
    code = compile(preamble + body, str(PATCH_DIR / PATCH_SPECS[1][1]), "exec")
    exec(code, namespace)  # noqa: S102  # pylint: disable=exec-used
    sender = namespace["Sender"]()
    sender.cache_family = request.param
    sender.token_database = SimpleNamespace(group_cache_families={"kv": {0: request.param}}, hash_block_size=128)
    sender._get_block_size = lambda _: 128
    sender._prepare_value = Mock(return_value=(0, 128, 0))
    sender.m_store = Mock()
    sender.update_kv_event = Mock()
    sender.enable_kv_event = True
    sender.kv_role = "kv_producer"
    return sender


@pytest.mark.parametrize("exists", [[False, False], [True, False], [True, True]])
def test_cpu_events_only_report_this_workers_confirmed_blocks(cpu_sender, exists):
    """A completed TP shard must not advertise another worker's pending writes."""
    from types import SimpleNamespace

    cpu_sender.lookup = lambda _: exists
    request = SimpleNamespace(
        block_hashes=["h0", "h1", "h2", "h3"],
        token_ids=list(range(512)),
        original_block_size=128,
    )
    compress_ratio = int(cpu_sender.cache_family[1:])
    logical_starts = [0, 256]
    physical_starts = [start // compress_ratio for start in logical_starts]
    physical_ends = [end // compress_ratio for end in [128, 384]]
    cpu_sender.send(request, physical_starts, physical_ends)
    selected_blocks = [0, 2]
    if cpu_sender.cache_family == "c1":
        selected_blocks = [index * 2 for index, hit in enumerate(exists) if not hit]
    if not selected_blocks:
        cpu_sender.update_kv_event.assert_not_called()
        cpu_sender.m_store.put.assert_not_called()
        return
    events = cpu_sender.update_kv_event.call_args.args[0]
    assert [event.block_hashes for event in events] == [[f"h{index}"] for index in selected_blocks]
    assert [event.parent_block_hash for event in events] == [
        None if index == 0 else f"h{index - 1}" for index in selected_blocks
    ]
    assert [event.token_ids for event in events] == [
        list(range(index * 128, (index + 1) * 128)) for index in selected_blocks
    ]
    if all(exists):
        cpu_sender.m_store.put.assert_not_called()
    else:
        assert cpu_sender.m_store.put.call_args.args[0] == [
            str(start) for start, hit in zip(physical_starts, exists) if not hit
        ]


def test_cpu_events_not_published_when_put_fails(cpu_sender):
    from types import SimpleNamespace

    cpu_sender.lookup = lambda _: [False]
    cpu_sender.m_store.put.side_effect = RuntimeError("store unavailable")
    request = SimpleNamespace(block_hashes=["h0"], token_ids=list(range(128)), original_block_size=128)
    with pytest.raises(RuntimeError, match="store unavailable"):
        cpu_sender.send(request, [0], [128])
    cpu_sender.update_kv_event.assert_not_called()


@pytest.mark.parametrize("starts,ends", [([], []), ([1], [128]), ([0], [64])])
def test_cpu_events_skip_empty_or_partial_grains(cpu_sender, starts, ends):
    from types import SimpleNamespace

    cpu_sender.lookup = lambda keys: [True] * len(keys)
    request = SimpleNamespace(block_hashes=["h0"], token_ids=list(range(128)), original_block_size=128)
    compress_ratio = int(cpu_sender.cache_family[1:])
    physical_starts = [(start + compress_ratio - 1) // compress_ratio for start in starts]
    physical_ends = [end // compress_ratio for end in ends]
    cpu_sender.send(request, physical_starts, physical_ends)
    cpu_sender.update_kv_event.assert_not_called()


def test_old_cpu_patch_marker_is_not_accepted_as_fixed(tmp_path):
    target = tmp_path / "kv_transfer.py"
    for old_marker in (
        "# motor-patch: cpu kv events main-attention grain (do not remove)",
        "# motor-patch: cpu kv events main-attention grain v2 (do not remove)",
        "# motor-patch: cpu kv events c4+c128 grain v3 (do not remove)",
        "# motor-patch: cpu kv events c1+c4+c128 grain v4 (do not remove)",
        "# motor-patch: cpu kv events c1+c4+c128 grain v5 (do not remove)",
        "# motor-patch: cpu kv events all compression grains v6 (do not remove)",
    ):
        target.write_text(f"{old_marker}\n")
        assert not is_patched(str(target), PATCH_SPECS[1][2])
        assert not is_patched(str(target), CPU_EVENT_SPEC_V030[2])


def test_v030_is_supported_and_selects_dedicated_cpu_patch():
    assert "0.30.0" in TARGET_VLLM_VERSIONS
    assert patch_specs_for_version("0.25.1+empty") == PATCH_SPECS
    assert patch_specs_for_version("0.30.0+empty") == [PATCH_SPECS[0], CPU_EVENT_SPEC_V030]
    assert patch_specs_for_version("0.30.0-rc1") == [PATCH_SPECS[0], CPU_EVENT_SPEC_V030]


def test_v032_is_supported_and_selects_version_dir_patches():
    assert "0.32.0" in TARGET_VLLM_VERSIONS
    expected = [METADATA_SPEC_V032, CONNECTOR_SPEC_V032, CPU_EVENT_SPEC_V032]
    assert patch_specs_for_version("0.32.0+empty") == expected
    assert patch_specs_for_version("0.32.0-rc1") == expected
    for _, patch_name, _ in expected:
        assert (PATCH_DIR / patch_name).is_file()


def _v030_repo() -> Path | None:
    candidates = [
        MOTOR_ROOT.parent.parent / "cmb" / "vllm-ascend",
        Path("/mnt/l00517252/cmb/vllm-ascend"),
    ]
    for repo in candidates:
        if (repo / ".git").exists() or (repo / ".git").is_file():
            return repo
    return None


def _git_show(repo: Path, spec: str) -> str | None:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo), "show", spec],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 and result.stdout else None


def test_v030_unique_union_and_cpu_grain_patches_apply(tmp_path):
    repo = _v030_repo()
    if repo is None:
        pytest.skip("vllm-ascend 0.30.0 repo not available")
    connector_src = _git_show(
        repo,
        "origin/releases/v0.30.0:vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py",
    )
    transfer_src = _git_show(
        repo,
        "origin/releases/v0.30.0:vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py",
    )
    if connector_src is None or transfer_src is None:
        pytest.skip("origin/releases/v0.30.0 not present in local vllm-ascend")

    connector = tmp_path / "ascend_store_connector.py"
    connector.write_text(connector_src)
    assert apply_patch(str(connector), str(PATCH_DIR / PATCH_SPECS[0][1]), MARKER)
    connector_text = connector.read_text(encoding="utf-8")
    ast.parse(connector_text)
    assert "unique_events = list(dict.fromkeys(self._aggregator.get_all_events()))" in connector_text
    assert apply_patch(str(connector), str(PATCH_DIR / PATCH_SPECS[0][1]), MARKER)

    _, patch_name, marker = CPU_EVENT_SPEC_V030
    transfer = tmp_path / "kv_transfer.py"
    transfer.write_text(transfer_src)
    assert apply_patch(str(transfer), str(PATCH_DIR / patch_name), marker)
    transfer_text = transfer.read_text(encoding="utf-8")
    ast.parse(transfer_text)
    assert marker in transfer_text
    assert "group_block_size * compress_ratio" in transfer_text
    assert "if not missing_indices:\n                continue" not in transfer_text
    assert apply_patch(str(transfer), str(PATCH_DIR / patch_name), marker)


@pytest.fixture(params=["c1", "c2", "c4", "c128"])
def cpu_sender_v030(request):
    """Execute the v0.30.0 patched sending path without an installed NPU engine."""
    import logging
    from types import SimpleNamespace
    from unittest.mock import Mock

    patch_text = (PATCH_DIR / CPU_EVENT_SPEC_V030[1]).read_text()
    postimage = "\n".join(
        line[1:] for line in patch_text.splitlines() if line.startswith((" ", "+")) and not line.startswith("+++")
    )
    body = postimage[
        postimage.index("            missing_indices = self._get_missing_indices") : postimage.index(
            "    def _build_cpu_stored_events"
        )
    ]
    helper = postimage[
        postimage.index("    def _build_cpu_stored_events") : postimage.index("class KVCacheStoreRecvingThread(")
    ]
    preamble = """class Sender:
    def send(self, req_meta, starts, ends):
        for group_id in [0]:
            token_len = len(req_meta.token_ids)
            req_id = "test"
            group_block_size = 32
            keys = [str(start) for start in starts]
            block_hashes = key_block_ids = block_ids = list(starts)
            current_event = None
            if not keys:
                return
"""
    namespace = {
        "ReqMeta": SimpleNamespace,
        "BlockStored": SimpleNamespace,
        "logger": logging.getLogger(__name__),
        "get_block_hashes": lambda hashes, *_: hashes,
        "maybe_convert_block_hash": lambda value: value,
    }
    code = compile(preamble + body + "\n" + helper, str(PATCH_DIR / CPU_EVENT_SPEC_V030[1]), "exec")
    exec(code, namespace)  # noqa: S102  # pylint: disable=exec-used
    sender = namespace["Sender"]()
    sender.cache_family = request.param
    sender.token_database = SimpleNamespace(group_cache_families={"kv": {0: request.param}}, hash_block_size=32)
    sender._get_missing_indices = lambda keys, require_exists_check=False: [
        index for index, exists in enumerate(sender.lookup(keys)) if not exists
    ]
    sender._prepare_value = Mock(return_value=(0, 32, 0))
    sender.m_store = Mock()
    sender.update_kv_event = Mock()
    sender.enable_kv_event = True
    sender.kv_role = "kv_producer"
    return sender


@pytest.mark.parametrize("exists", [[False, False], [True, False], [True, True]])
def test_v030_cpu_events_use_logical_ranges_and_effective_grain(cpu_sender_v030, exists):
    from types import SimpleNamespace

    cpu_sender_v030.lookup = lambda _: exists
    compress_ratio = int(cpu_sender_v030.cache_family[1:])
    if compress_ratio == 1:
        starts, ends = [0, 64], [32, 96]
        request = SimpleNamespace(
            block_hashes=["h0", "h1", "h2", "h3"],
            token_ids=list(range(128)),
            original_block_size=32,
        )
        selected_blocks = [index * 2 for index, hit in enumerate(exists) if not hit]
        event_block_size = 32
        grain = 32
    else:
        grain = 32 * compress_ratio
        starts, ends = [0, grain], [grain, grain * 2]
        request = SimpleNamespace(
            block_hashes=["h0", "h1"],
            token_ids=list(range(grain * 2)),
            original_block_size=32,
        )
        selected_blocks = [0, 1]
        event_block_size = grain
    cpu_sender_v030.send(request, starts, ends)
    if not selected_blocks:
        cpu_sender_v030.update_kv_event.assert_not_called()
        cpu_sender_v030.m_store.put.assert_not_called()
        return
    events = cpu_sender_v030.update_kv_event.call_args.args[0]
    assert [event.block_hashes for event in events] == [[f"h{index}"] for index in selected_blocks]
    assert [event.block_size for event in events] == [event_block_size] * len(selected_blocks)
    assert [event.token_ids for event in events] == [
        list(range(index * grain, (index + 1) * grain)) for index in selected_blocks
    ]
    if all(exists):
        cpu_sender_v030.m_store.put.assert_not_called()
    else:
        assert cpu_sender_v030.m_store.put.call_args.args[0] == [
            str(start) for start, hit in zip(starts, exists) if not hit
        ]


def test_v030_cpu_events_not_published_when_put_fails(cpu_sender_v030):
    from types import SimpleNamespace

    cpu_sender_v030.lookup = lambda _: [False]
    cpu_sender_v030.m_store.put.side_effect = RuntimeError("store unavailable")
    request = SimpleNamespace(block_hashes=["h0"], token_ids=list(range(32)), original_block_size=32)
    with pytest.raises(RuntimeError, match="store unavailable"):
        cpu_sender_v030.send(request, [0], [32])
    cpu_sender_v030.update_kv_event.assert_not_called()


@pytest.mark.parametrize("starts,ends", [([], []), ([1], [32]), ([0], [16])])
def test_v030_cpu_events_skip_empty_or_partial_grains(cpu_sender_v030, starts, ends):
    from types import SimpleNamespace

    cpu_sender_v030.lookup = lambda keys: [True] * len(keys)
    request = SimpleNamespace(block_hashes=["h0"], token_ids=list(range(32)), original_block_size=32)
    cpu_sender_v030.send(request, starts, ends)
    cpu_sender_v030.update_kv_event.assert_not_called()


def _v032_patch_paths() -> list[Path]:
    return [
        PATCH_DIR / patch_name for _, patch_name, _ in (METADATA_SPEC_V032, CONNECTOR_SPEC_V032, CPU_EVENT_SPEC_V032)
    ]


def test_v032_patch_hunk_headers_match_bodies():
    """Catch malformed unified-diff headers that break ``patch`` at EOF."""
    import re

    for patch_path in _v032_patch_paths():
        lines = patch_path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            if not line.startswith("@@"):
                index += 1
                continue
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            assert match, f"bad hunk header in {patch_path.name}: {line}"
            old_declared = int(match.group(2) or 1)
            new_declared = int(match.group(4) or 1)
            index += 1
            old_actual = new_actual = 0
            while index < len(lines) and not lines[index].startswith(("@@", "--- ", "+++ ")):
                body = lines[index]
                if body.startswith("+") and not body.startswith("+++"):
                    new_actual += 1
                elif body.startswith("-") and not body.startswith("---"):
                    old_actual += 1
                elif body.startswith("\\"):
                    pass
                else:
                    old_actual += 1
                    new_actual += 1
                index += 1
            assert (old_actual, new_actual) == (old_declared, new_declared), (
                f"{patch_path.name} hunk {line}: declared "
                f"{old_declared}/{new_declared}, actual {old_actual}/{new_actual}"
            )


def _v032_repo() -> Path | None:
    candidates = [
        MOTOR_ROOT.parent / "vllm-ascend",
        MOTOR_ROOT.parent.parent / "cmb" / "vllm-ascend",
        Path("/mnt/l00517252/cmb/vllm-ascend"),
    ]
    for repo in candidates:
        if (repo / ".git").exists() or (repo / ".git").is_file():
            return repo
    return None


def test_v032_unique_union_metadata_and_cpu_grain_patches_apply(tmp_path):
    repo = _v032_repo()
    if repo is None:
        pytest.skip("vllm-ascend repo not available")
    specs = [
        (
            "releases/v0.32.0rc:vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/metadata.py",
            METADATA_SPEC_V032,
        ),
        (
            "releases/v0.32.0rc:vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py",
            CONNECTOR_SPEC_V032,
        ),
        (
            "releases/v0.32.0rc:vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py",
            CPU_EVENT_SPEC_V032,
        ),
    ]
    sources = []
    for git_spec, _ in specs:
        content = _git_show(repo, git_spec)
        if content is None:
            content = _git_show(repo, f"origin/{git_spec}")
        if content is None:
            pytest.skip(f"releases/v0.32.0rc not present for {git_spec}")
        sources.append(content)

    for content, (_, (rel_path, patch_name, marker)) in zip(sources, specs, strict=True):
        target = tmp_path / Path(rel_path).name
        target.write_text(content)
        assert apply_patch(str(target), str(PATCH_DIR / patch_name), marker)
        text = target.read_text(encoding="utf-8")
        ast.parse(text)
        assert marker in text
        assert apply_patch(str(target), str(PATCH_DIR / patch_name), marker)


@pytest.fixture(params=["c1", "c2", "c4", "c128"])
def cpu_publisher_v032(request):
    """Execute the v0.32.0 CPU-event helpers without an installed NPU engine."""
    import logging
    from types import SimpleNamespace
    from unittest.mock import Mock

    meta_post = "\n".join(
        line[1:]
        for line in (PATCH_DIR / METADATA_SPEC_V032[1]).read_text().splitlines()
        if line.startswith((" ", "+")) and not line.startswith("+++")
    )
    infer_fn = meta_post[
        meta_post.index("def infer_cache_family_ratio") : meta_post.index("\ndef get_block_hashes")
        if "\ndef get_block_hashes" in meta_post
        else meta_post.index("\ndef maybe_convert_block_hash")
        if "\ndef maybe_convert_block_hash" in meta_post
        else len(meta_post)
    ]
    # metadata patch only adds infer_cache_family_ratio; cut at next blank-def if present
    if "\ndef " in infer_fn[4:]:
        infer_fn = infer_fn[: infer_fn.index("\ndef ", 4)]

    patch_text = (PATCH_DIR / CPU_EVENT_SPEC_V032[1]).read_text()
    postimage = "\n".join(
        line[1:] for line in patch_text.splitlines() if line.startswith((" ", "+")) and not line.startswith("+++")
    )
    helpers = postimage[
        postimage.index("    def _cpu_event_ranges") : postimage.index("    def _handle_stored_request")
    ]
    preamble = """
class Sender:
    pass
"""
    namespace = {
        "ReqMeta": SimpleNamespace,
        "BlockStored": SimpleNamespace,
        "logger": logging.getLogger(__name__),
        "get_block_hashes": lambda hashes, effective, _hash: hashes,
        "maybe_convert_block_hash": lambda value: value,
    }
    exec(compile(infer_fn, str(PATCH_DIR / METADATA_SPEC_V032[1]), "exec"), namespace)  # noqa: S102  # pylint: disable=exec-used
    exec(compile(preamble + helpers, str(PATCH_DIR / CPU_EVENT_SPEC_V032[1]), "exec"), namespace)  # noqa: S102  # pylint: disable=exec-used
    sender = namespace["Sender"]()
    sender.cache_family = request.param
    sender.token_database = SimpleNamespace(group_cache_families={"kv": {0: request.param}}, hash_block_size=32)
    sender._get_block_size = lambda _group_id: 32 * max(1, int(request.param[1:])) if request.param != "c1" else 32
    if request.param == "c1":
        sender._get_block_size = lambda _group_id: 32
    else:
        ratio = int(request.param[1:])
        sender._get_block_size = lambda _group_id, r=ratio: 32 * r
    sender.get_event_token_ids = lambda req_meta, start, end: req_meta.token_ids[start:end]
    sender.update_kv_event = Mock()
    sender.enable_kv_event = True
    sender.infer_cache_family_ratio = namespace["infer_cache_family_ratio"]
    # helpers close over module-global infer_cache_family_ratio
    return sender


@pytest.mark.parametrize("exists", [[False, False], [True, False], [True, True]])
def test_v032_cpu_events_use_effective_grain_and_lookup_hits(cpu_publisher_v032, exists):
    from types import SimpleNamespace

    compress_ratio = int(cpu_publisher_v032.cache_family[1:])
    effective = 32 if compress_ratio == 1 else 32 * compress_ratio
    selected_ranges = [(0, effective), (effective, effective * 2)]
    missing_indices = [index for index, hit in enumerate(exists) if not hit]
    request = SimpleNamespace(
        block_hashes=[f"h{index}" for index in range(2 if compress_ratio > 1 else 4)],
        token_ids=list(range(effective * 2 if compress_ratio > 1 else 128)),
        original_block_size=32,
    )
    if compress_ratio == 1:
        # uncompressed: only missing ranges publish; grains are original_block_size
        selected_ranges = [(0, 32), (64, 96)]
        request = SimpleNamespace(
            block_hashes=["h0", "h1", "h2", "h3"],
            token_ids=list(range(128)),
            original_block_size=32,
        )
        cpu_publisher_v032._get_block_size = lambda _group_id: 32

    cpu_publisher_v032._publish_cpu_stored_events(
        request,
        0,
        len(request.token_ids),
        selected_ranges,
        missing_indices,
        None if missing_indices == [0, 1] or not missing_indices else None,
    )
    # logical_success=None means full selected/missing path after successful put
    if compress_ratio > 1:
        # compressed includes lookup hits via selected_ranges when logical_success is None
        cpu_publisher_v032._publish_cpu_stored_events(
            request, 0, len(request.token_ids), selected_ranges, missing_indices, None
        )
        events = cpu_publisher_v032.update_kv_event.call_args.args[0]
        assert [event.block_hashes for event in events] == [["h0"], ["h1"]]
        assert all(event.block_size == effective for event in events)
        return

    cpu_publisher_v032.update_kv_event.reset_mock()
    cpu_publisher_v032._publish_cpu_stored_events(
        request, 0, len(request.token_ids), selected_ranges, missing_indices, None
    )
    if not missing_indices:
        # uncompressed with no missing keys still publishes nothing from empty missing_indices
        # when logical_success is None and missing_indices empty → stored_ranges empty
        cpu_publisher_v032.update_kv_event.assert_not_called()
        return
    events = cpu_publisher_v032.update_kv_event.call_args.args[0]
    selected_blocks = [0 if index == 0 else 2 for index in missing_indices]
    assert [event.block_hashes for event in events] == [[f"h{block}"] for block in selected_blocks]
    assert all(event.block_size == 32 for event in events)


def test_v032_cpu_events_not_published_when_partial_put_fails(cpu_publisher_v032):
    from types import SimpleNamespace

    request = SimpleNamespace(block_hashes=["h0", "h1"], token_ids=list(range(64)), original_block_size=32)
    cpu_publisher_v032._get_block_size = lambda _group_id: 32
    cpu_publisher_v032.cache_family = "c1"
    cpu_publisher_v032.token_database.group_cache_families["kv"][0] = "c1"
    cpu_publisher_v032._publish_cpu_stored_events(
        request,
        0,
        64,
        [(0, 32), (32, 64)],
        [0, 1],
        [False, False],
    )
    cpu_publisher_v032.update_kv_event.assert_not_called()


def test_v032_cpu_events_skip_partial_grains(cpu_publisher_v032):
    from types import SimpleNamespace

    cpu_publisher_v032.cache_family = "c1"
    cpu_publisher_v032.token_database.group_cache_families["kv"][0] = "c1"
    cpu_publisher_v032._get_block_size = lambda _group_id: 32
    request = SimpleNamespace(block_hashes=["h0"], token_ids=list(range(32)), original_block_size=32)
    cpu_publisher_v032._publish_cpu_stored_events(request, 0, 32, [(0, 16)], [0], None)
    cpu_publisher_v032.update_kv_event.assert_not_called()
