# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import base64
import io
import os
import tempfile

import pytest
from PIL import Image

from motor.common.utils import image_utils


def _create_image_bytes(fmt: str, size=(100, 80)) -> bytes:
    """Create in-memory image bytes for a given format and size."""
    img = Image.new("RGB", size, color=(10, 20, 30))
    with io.BytesIO() as buf:
        img.save(buf, format=fmt)
        return buf.getvalue()


def test_parse_png_size():
    """Test that PNG size can be parsed from the image header."""
    w, h = 123, 45
    data = _create_image_bytes("PNG", size=(w, h))
    got_w, got_h = image_utils.parse_png_size(data)
    assert (got_w, got_h) == (w, h)


def test_parse_jpeg_size():
    """Test that JPEG size can be parsed from the image header."""
    w, h = 77, 99
    data = _create_image_bytes("JPEG", size=(w, h))
    got_w, got_h = image_utils.parse_jpeg_size(data)
    assert (got_w, got_h) == (w, h)


def test_parse_jpeg_size_invalid_raises():
    """Test that invalid JPEG data raises a ValueError."""
    with pytest.raises(ValueError):
        image_utils.parse_jpeg_size(b"not a jpeg")


def test_fast_get_hw_with_base64():
    """Test fast_get_hw by decoding a base64 data URI and reading dimensions via PIL."""
    w, h = 64, 48
    data = _create_image_bytes("JPEG", size=(w, h))
    b64 = base64.b64encode(data).decode("ascii")
    uri = f"data:image/jpeg;base64,{b64}"
    got_w, got_h = image_utils.fast_get_hw(uri)
    assert (got_w, got_h) == (w, h)


def test_get_hw_from_local_png_and_file_uri():
    """Test get_hw_from_local for PNG files and file:// URI paths."""
    w, h = 31, 65
    data = _create_image_bytes("PNG", size=(w, h))
    # write to a temp file
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(data)

        got_w, got_h = image_utils.get_hw_from_local(path)
        assert (got_w, got_h) == (w, h)

        # file:// scheme
        got_w2, got_h2 = image_utils.get_hw_from_local("file://" + path)
        assert (got_w2, got_h2) == (w, h)
    finally:
        os.remove(path)


def test_get_mul_token_for_base64_and_local():
    """Test get_mul_token for both base64 and local image inputs."""
    w, h = 100, 100
    data = _create_image_bytes("JPEG", size=(w, h))
    # base64
    b64 = base64.b64encode(data).decode("ascii")
    uri = f"data:image/jpeg;base64,{b64}"
    mul = image_utils.get_mul_token(uri)
    expected = ( (h + 31) // 32 ) * ( (w + 31) // 32 )
    assert mul == expected

    # local file
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(data)
        mul2 = image_utils.get_mul_token(path)
        assert mul2 == expected
    finally:
        os.remove(path)
