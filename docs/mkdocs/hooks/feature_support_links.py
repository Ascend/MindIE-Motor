# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Resolve the feature support tables' raw HTML links for the MkDocs site."""

import posixpath
import re
from urllib.parse import unquote, urlsplit

from mkdocs.exceptions import PluginError

FEATURE_SUPPORT_PAGE = "user_guide/features/supported_features.md"
ANCHOR_HREF = re.compile(
    r'''(?P<prefix><a(?=\s)(?:[^"'<>]|"[^"]*"|'[^']*')*?\s+href\s*=\s*)'''
    r'''(?P<quote>["'])(?P<url>[^"']+)(?P=quote)''',
    re.IGNORECASE,
)


def on_page_content(html, *, page, files, **_kwargs):
    """Map repository-relative links to the corresponding published pages."""
    if page.file.src_uri != FEATURE_SUPPORT_PAGE:
        return html

    # MkDocs rewrites Markdown links but skips raw anchors in HTML tables.
    source_dir = posixpath.dirname(page.file.src_uri)

    def replace_link(match):
        href = match["url"].strip()
        url = urlsplit(href)
        path = unquote(url.path)
        if url.scheme or url.netloc or path.startswith("/") or not path.endswith(".md"):
            return match[0]

        source_path = posixpath.normpath(posixpath.join(source_dir, path))
        target = files.get_file_from_path(source_path)
        if target is None:
            raise PluginError(f"Broken feature link in {page.file.src_uri}: {href}")
        suffix = href[len(url.path) :]
        quote = match["quote"]
        return f'{match["prefix"]}{quote}{target.url_relative_to(page.file)}{suffix}{quote}'

    return ANCHOR_HREF.sub(replace_link, html)
