# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Keep model links local in source and versioned in the published documentation."""

import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

# Leave fenced examples and inline code unchanged. Model links themselves may
# have inline-code labels; only their parenthesized destinations are replaced.
LINK_OR_CODE = re.compile(
    r"(?P<code>```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)"
    r"|(?<=\]\()(?P<target>(?:\.\./)+infer_engines/[^\s)]+)(?=\))"
)


def on_page_markdown(markdown: str, *, page, config, **_kwargs) -> str:
    """Resolve model links against the checked-out tree before emitting URLs."""
    repo_root = Path(config.config_file_path).resolve().parent
    models_root = repo_root / "infer_engines"
    source_dir = Path(page.file.abs_src_path).parent
    ref = os.environ.get("READTHEDOCS_GIT_IDENTIFIER", "master")
    if os.environ.get("READTHEDOCS_VERSION_TYPE") == "external":
        ref = os.environ["READTHEDOCS_GIT_COMMIT_HASH"]
    repo_url = config["repo_url"].rstrip("/")

    def replace(match: re.Match) -> str:
        destination = match.group("target")
        if destination is None:
            return match.group(0)
        parts = urlsplit(destination)
        target = (source_dir / unquote(parts.path)).resolve()
        if not target.is_relative_to(models_root) or not target.exists():
            raise ValueError(f"Invalid model configuration link in {page.file.src_uri}: {destination}")
        kind = "tree" if target.is_dir() else "blob"
        relative_path = quote(target.relative_to(repo_root).as_posix(), safe="/")
        suffix = (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
        return f"{repo_url}/{kind}/{quote(ref, safe='')}/{relative_path}{suffix}"

    return LINK_OR_CODE.sub(replace, markdown)
