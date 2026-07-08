#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Pre-commit hook: clean and verify Python file headers.

Rules applied (auto-fix):
1. Remove ``# -*- coding: utf-8 -*-`` — useless in Python 3 (UTF-8 is the default).
2. Remove ``#!/usr/bin/env python3`` shebangs — not needed for library / non-script code.
3. Ensure every file carries the Mulan PSL v2 license block.
   Missing license → the block is prepended automatically.

Excluded: generated protobuf/gRPC files, build artifacts, egg-info, __pycache__.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Mulan PSL v2 license block (exact text)
# ---------------------------------------------------------------------------
LICENSE_LINES: list[str] = [
    "# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.\n",
    "# MindIE is licensed under Mulan PSL v2.\n",
    "# You can use this software according to the terms and conditions of the Mulan PSL v2.\n",
    "# You may obtain a copy of Mulan PSL v2 at:\n",
    "#         http://license.coscl.org.cn/MulanPSL2\n",
    '# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,\n',
    "# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,\n",
    "# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.\n",
    "# See the Mulan PSL v2 for more details.\n",
]

# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------
# PEP 263 encoding declarations (various forms)
_CODING_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:-?\*-?[ \t]*)?coding[=:][ \t]*utf-?8[ \t]*(?:-?\*-?)?[ \t]*$",
    re.IGNORECASE,
)

# Shebang lines
_SHEBANG_RE = re.compile(r"^#!.*\bpython")

# Detects the start of the Mulan license block
_LICENSE_START_RE = re.compile(r"^# Copyright \(c\) Huawei Technologies")

# Files / path patterns to skip entirely
_SKIP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"_pb2\.py$"),  # protobuf generated
    re.compile(r"_pb2_grpc\.py$"),  # gRPC generated
    re.compile(r"_grpc\.py$"),  # alternative gRPC naming
    re.compile(r"^build/"),
    re.compile(r"\.egg-info/"),
    re.compile(r"__pycache__/"),
]


# ---------------------------------------------------------------------------
def _should_skip(rel_path: str) -> bool:
    """Return True for generated / build files that should not be touched."""
    return any(pattern.search(rel_path) for pattern in _SKIP_PATTERNS)


def _has_license(lines: list[str]) -> bool:
    """Return True if *lines* already contains the Mulan PSL v2 block."""
    for line in lines:
        if _LICENSE_START_RE.match(line):
            return True
    return False


def _strip_leading(lines: list[str]) -> tuple[list[str], int, int]:
    """Remove coding and shebang lines from the start of *lines*.

    Returns ``(cleaned_lines, removed_count, first_content_idx)``.
    """
    kept: list[str] = []
    removed = 0
    first_content = 0

    for i, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        is_coding = bool(_CODING_RE.match(stripped))
        is_shebang = bool(_SHEBANG_RE.match(stripped))

        if is_coding or is_shebang:
            removed += 1
            continue

        # Stop only stripping at the very beginning — after the first
        # non-coding, non-shebang line we keep everything.
        kept.extend(lines[i:])
        first_content = i
        break
    else:
        # All lines were stripped
        kept = []

    return kept, removed, first_content


def process_file(path: Path) -> bool:
    """Check and fix *path* in-place.  Returns True when the file was modified."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    if not source.strip():
        # Empty file — skip
        return False

    lines = source.splitlines(keepends=True)
    modified = False

    # ---- 1. Strip coding & shebang lines from the very beginning ----
    lines, removed, first_content = _strip_leading(lines)
    if removed:
        modified = True

    # ---- 2. Ensure license block exists (always at the top) ----
    if not _has_license(lines):
        license_text = list(LICENSE_LINES)
        # Add a blank line separator after the license block
        if lines and lines[0].strip():
            license_text.append("\n")
        lines[:0] = license_text
        modified = True

    # ---- 3. Ensure there's exactly one blank line after the license block ----
    # Find license end
    license_end = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "# See the Mulan PSL v2 for more details.":
            license_end = i + 1
            break

    if license_end > 0:
        # Normalize whitespace after license: collapse blank lines → exactly one blank
        # line, then content (including any module-level comments) resumes.
        blank_start = license_end
        while blank_start < len(lines) and lines[blank_start].strip() == "":
            blank_start += 1

        desired = lines[:license_end] + ["\n"]
        if blank_start < len(lines):
            desired.extend(lines[blank_start:])

        if desired != lines:
            lines = desired
            modified = True

    if not modified:
        return False

    path.write_text("".join(lines), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clean/verify Python file headers (coding, shebang, Mulan PSL v2 license).",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Files to check (passed by pre-commit).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Only report issues, do not modify files (for CI).",
    )
    args = parser.parse_args(argv)

    if not args.files:
        return 0

    modified_count = 0
    skipped_count = 0
    error_count = 0

    for file_arg in args.files:
        path = Path(file_arg)
        if not path.is_file() or path.suffix != ".py":
            continue

        rel = str(path).replace("\\", "/")
        if _should_skip(rel):
            continue

        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            print(f"check_header: cannot read {rel}", file=sys.stderr)
            error_count += 1
            continue

        if not source.strip():
            continue

        lines = source.splitlines(keepends=True)

        # Detect issues without modifying
        has_coding = any(_CODING_RE.match(line.rstrip("\n\r")) for line in lines[:5])
        has_shebang = any(_SHEBANG_RE.match(line.rstrip("\n\r")) for line in lines[:3])
        has_license = _has_license(lines)

        issues: list[str] = []
        if has_coding:
            issues.append("has coding declaration")
        if has_shebang:
            issues.append("has shebang")
        if not has_license:
            issues.append("missing Mulan PSL v2 license")

        if not issues:
            continue

        if args.check:
            # Report only
            print(f"{rel}: {', '.join(issues)}")
            error_count += 1
        else:
            # Auto-fix
            if process_file(path):
                print(f"check_header: fixed {rel} — {', '.join(issues)}")
                modified_count += 1
            else:
                skipped_count += 1

    if modified_count:
        print(f"check_header: fixed {modified_count} file(s)")
    if error_count:
        print(f"check_header: {error_count} file(s) need fixes (re-run without --check to auto-fix)")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
