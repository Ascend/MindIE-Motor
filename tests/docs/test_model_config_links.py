# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Check source links and published model links against the actual checkout."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from docs.mkdocs.hooks.model_config_links import on_page_markdown

ROOT = Path(__file__).resolve().parents[2]


class DocsConfig(dict):
    """Minimal MkDocs config for testing without optional documentation packages."""

    config_file_path = ROOT / "mkdocs.yml"


@pytest.mark.parametrize("ref", ["master", "v3.2.0"])
def test_model_links_in_documentation_resolve_and_use_build_version(monkeypatch, ref):
    monkeypatch.setenv("READTHEDOCS_GIT_IDENTIFIER", ref)
    monkeypatch.delenv("READTHEDOCS_VERSION_TYPE", raising=False)
    config = DocsConfig(repo_url="https://gitcode.com/Ascend/MindIE-Motor")
    converted = 0
    for source in (ROOT / "docs/zh").rglob("*.md"):
        markdown = source.read_text()
        page = SimpleNamespace(file=SimpleNamespace(abs_src_path=source, src_uri=str(source.relative_to(ROOT))))
        output = on_page_markdown(markdown, page=page, config=config)
        if output != markdown:
            converted += 1
            assert f"/blob/{ref}/model_configs/" in output or f"/tree/{ref}/model_configs/" in output
        assert "/blob/master/model_configs/" not in markdown
    assert converted >= 5


def test_bad_model_links_fail_build_but_code_and_other_links_are_unchanged(monkeypatch):
    monkeypatch.setenv("READTHEDOCS_GIT_IDENTIFIER", "v3.2.0")
    monkeypatch.delenv("READTHEDOCS_VERSION_TYPE", raising=False)
    config = DocsConfig(repo_url="https://gitcode.com/Ascend/MindIE-Motor")
    page = SimpleNamespace(
        file=SimpleNamespace(
            abs_src_path=ROOT / "docs/zh/user_guide/features/kvcache_affinity.md", src_uri="kvcache_affinity.md"
        )
    )
    invalid = "[config](../../../../../model_configs/vllm/user_config.json)"
    with pytest.raises(ValueError, match="Invalid model configuration link"):
        on_page_markdown(invalid, page=page, config=config)
    unchanged = f"```markdown\n{invalid}\n```\n`{invalid}`\n[docs](../quick_start.md)\n"
    assert on_page_markdown(unchanged, page=page, config=config) == unchanged
    valid = "[`config`](../../../../model_configs/vllm/user_config.json#example)"
    assert "/blob/v3.2.0/model_configs/vllm/user_config.json#example" in on_page_markdown(
        valid, page=page, config=config
    )
    monkeypatch.setenv("READTHEDOCS_VERSION_TYPE", "external")
    monkeypatch.setenv("READTHEDOCS_GIT_IDENTIFIER", "1093")
    monkeypatch.setenv("READTHEDOCS_GIT_COMMIT_HASH", "abc123")
    assert "/blob/abc123/model_configs/" in on_page_markdown(valid, page=page, config=config)
