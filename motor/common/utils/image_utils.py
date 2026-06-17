# -*- coding: utf-8 -*-
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
import math
from PIL import Image


# Set of JPEG Start Of Frame (SOF) markers.
# These markers indicate the beginning of the frame header, 
# which contains image dimensions (width and height).
SOF_MARKERS = {
    0xC0,  # SOF0 (Baseline DCT)
    0xC1,  # SOF1 (Extended Sequential DCT)
    0xC2,  # SOF2 (Progressive DCT)
    0xC3,  # SOF3 (Lossless Sequential)
    0xC5,  # SOF5 (Differential Sequential)
    0xC6,  # SOF6 (Differential Progressive)
    0xC7,  # SOF7 (Differential Lossless)
    0xC9,  # SOF9 (Extended Sequential DCT, Arithmetic Coding)
    0xCA,  # SOF10 (Progressive DCT, Arithmetic Coding)
    0xCB,  # SOF11 (Lossless Sequential, Arithmetic Coding)
    0xCD,  # SOF13 (Differential Sequential, Arithmetic Coding)
    0xCE,  # SOF14 (Differential Progressive, Arithmetic Coding)
    0xCF,  # SOF15 (Differential Lossless, Arithmetic Coding)
}


def parse_jpeg_size(data: bytes):
    """
    Parses JPEG binary data to extract width and height by reading the SOF marker.
    This is faster than decoding the entire image as it only reads the header.
    
    Args:
        data: Raw JPEG byte data.
        
    Returns:
        A tuple (width, height).
        
    Raises:
        ValueError: If the data is not a valid JPEG or SOF marker is not found.
    """
    idx = 0
    length = len(data)

    # Check for JPEG magic number (SOI - Start Of Image)
    if length < 2 or data[0:2] != b"\xff\xd8":
        raise ValueError("Not a JPEG")

    # Start parsing after the SOI marker
    idx = 2
    while idx + 9 < length:
        # Look for the next marker prefix (0xFF)
        if data[idx] != 0xFF:
            idx += 1
            continue

        marker = data[idx + 1]

        # Handle padding bytes (0xFF followed by 0xFF)
        if marker == 0xFF:
            idx += 1
            continue

        # Check if this is a Start Of Frame marker containing dimensions
        if marker in SOF_MARKERS:
            # Structure of SOF segment:
            # [Marker(2)] [Length(2)] [Precision(1)] [Height(2)] [Width(2)] ...
            # Height is at offset +5, +6; Width is at offset +7, +8
            h = (data[idx + 5] << 8) | data[idx + 6]
            w = (data[idx + 7] << 8) | data[idx + 8]
            return w, h

        # Stop if we reach End Of Image (EOI) or Start Of Scan (SOS)
        # SOS indicates the start of compressed image data, so headers are done.
        if marker in (0xD9, 0xDA):
            break

        # Ensure there are enough bytes to read the segment length
        if idx + 3 >= length:
            break

        # Read the length of the current segment (includes the 2 bytes for length itself)
        seg_len = (data[idx + 2] << 8) | data[idx + 3]
        if seg_len < 2:
            break

        # Skip to the next segment
        idx += 2 + seg_len

    raise ValueError("JPEG SOF marker not found")


def parse_png_size(data: bytes):
    """
    Parses PNG binary data to extract width and height from the IHDR chunk.
    
    Args:
        data: Raw PNG byte data (must be at least 24 bytes).
        
    Returns:
        A tuple (width, height).
    """
    # PNG IHDR chunk structure:
    # Bytes 16-19: Width (Big Endian)
    # Bytes 20-23: Height (Big Endian)
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return w, h


def fast_get_hw(b64_str: str):
    """
    Quickly extracts image dimensions from a Base64 encoded data URI using PIL.
    Note: While 'fast' compared to full processing, it still decodes the header via PIL.
    
    Args:
        b64_str: A data URI string (e.g., "data:image/jpeg;base64,...").
        
    Returns:
        A tuple (width, height).
    """
    # Split the data URI to get only the base64 encoded part
    # Assumes format: "data:<mime>;base64,<encoded_data>"
    img_bytes = base64.b64decode(b64_str.split(",")[1])
    
    # Open image using PIL to get dimensions
    with io.BytesIO(img_bytes) as f:
        with Image.open(f) as img:
            return img.width, img.height


def get_hw_from_local(path: str):
    """
    Reads the first 64KB of a local image file to determine dimensions.
    Supports PNG and JPEG formats.
    
    Args:
        path: File path or file URI (starting with "file://").
        
    Returns:
        A tuple (width, height).
    """
    # Remove file:// protocol prefix if present
    if path.startswith("file://"):
        path = path[7:]

    # Read only the first 64KB, which is sufficient for headers of most images
    with open(path, "rb") as f:
        data = f.read(65536)

    # Check PNG signature
    if data.startswith(b"\x89PNG"):
        return parse_png_size(data)
    
    # Assume JPEG if not PNG
    return parse_jpeg_size(data)


def get_mul_token(img_url: str) -> float:
    """
    Calculates a token multiplier based on image dimensions.
    The formula divides the image into 32x32 patches and counts them.
    
    Args:
        img_url: A local file path, file URI, or base64 data URI.
        
    Returns:
        The calculated multiplier (float).
    """
    if img_url.startswith("data:image"):
        # Handle base64 encoded images
        h, w = fast_get_hw(img_url)
    else:
        # Handle local file paths
        h, w = get_hw_from_local(img_url)
    
    # Calculate number of 32x32 patches needed to cover the image
    # Note: The original code had a bug: 'mul_token' was used before assignment.
    # It should likely be 'mul_token = ...' or returned directly.
    mul_token = math.ceil(h / 32) * math.ceil(w / 32)
    
    return mul_token
