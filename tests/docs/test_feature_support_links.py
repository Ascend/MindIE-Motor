# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Verify the links generated from the feature support tables."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

mkdocs_exceptions = pytest.importorskip("mkdocs.exceptions")
feature_links = pytest.importorskip("docs.mkdocs.hooks.feature_support_links")

SOURCE_PATH = "user_guide/features/supported_features.md"
TARGET_PATH = "user_guide/features/inference_scheduling/pd_disaggregation.md"
TARGET_URL = "../inference_scheduling/pd_disaggregation/"


@pytest.fixture
def feature_page():
    page = SimpleNamespace(file=SimpleNamespace(src_uri=SOURCE_PATH))
    files = Mock()
    files.get_file_from_path.return_value.url_relative_to.return_value = TARGET_URL
    return page, files


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            '<a href="./inference_scheduling/pd_disaggregation.md">PD</a>',
            f'<a href="{TARGET_URL}">PD</a>',
        ),
        (
            '<a class="feature" href="inference_scheduling/pd_disaggregation.md#pd" target="_blank">PD</a>',
            f'<a class="feature" href="{TARGET_URL}#pd" target="_blank">PD</a>',
        ),
        (
            "<a data-href='example.md' href='./inference_scheduling/pd_disaggregation.md?x=1&amp;y=2#pd'>PD</a>",
            f"<a data-href='example.md' href='{TARGET_URL}?x=1&amp;y=2#pd'>PD</a>",
        ),
    ],
)
def test_rewrites_feature_links(before, after, feature_page):
    page, files = feature_page
    assert feature_links.on_page_content(before, page=page, files=files) == after
    files.get_file_from_path.assert_called_once_with(TARGET_PATH)
    files.get_file_from_path.return_value.url_relative_to.assert_called_once_with(page.file)


@pytest.mark.parametrize("href", ["https://example.com/page.md", "//example.com/page.md", "/page.md", "#pd"])
def test_other_urls_are_preserved(href, feature_page):
    page, files = feature_page
    html = f'<a href="{href}">PD</a>'
    assert feature_links.on_page_content(html, page=page, files=files) == html
    files.get_file_from_path.assert_not_called()


def test_missing_feature_reports_source_and_href(feature_page):
    page, files = feature_page
    files.get_file_from_path.return_value = None
    html = '<a href="missing.md#pd" title="PD">PD</a>'
    with pytest.raises(mkdocs_exceptions.PluginError, match=r"supported_features\.md: missing\.md#pd"):
        feature_links.on_page_content(html, page=page, files=files)


def test_other_pages_are_preserved(feature_page):
    page, files = feature_page
    page.file.src_uri = "user_guide/quick_start.md"
    html = '<a href="missing.md">PD</a>'
    assert feature_links.on_page_content(html, page=page, files=files) == html
    files.get_file_from_path.assert_not_called()
