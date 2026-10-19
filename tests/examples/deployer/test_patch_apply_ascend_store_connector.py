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
    CPU_EVENT_SPEC_V030,
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
        "setup_motor_log_path setup_ascend_work_path setup_ascend_cache_path set_prefill_env"
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
