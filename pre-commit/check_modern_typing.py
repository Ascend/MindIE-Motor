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

"""Pre-commit hook: detect and auto-fix old-style Python typing annotations.

Transforms (Python 3.10+ native syntax):
  ``Optional[X]``       → ``X | None``
  ``Union[X, Y]``       → ``X | Y``
  ``Dict[K, V]``        → ``dict[K, V]``
  ``List[X]``           → ``list[X]``
  ``Tuple[X, ...]``     → ``tuple[X, ...]``
  ``Set[X]``            → ``set[X]``
  ``FrozenSet[X]``      → ``frozenset[X]``
  ``Type[X]``           → ``type[X]``

Also cleans up ``from typing import ...`` lines when imports become unused.
Excludes ``deployer/`` (out of scope per project policy).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Mapping of old-style typing names → native replacement strategy
# ---------------------------------------------------------------------------
# Each entry: (is_special_form, replacement_prefix)
# - is_special_form: True for Optional/Union (special rewrite), False for simple rename
# - replacement_prefix: the lowercase builtin name for simple renames

SIMPLE_RENAMES: dict[str, str] = {
    "Dict": "dict",
    "List": "list",
    "Tuple": "tuple",
    "Set": "set",
    "FrozenSet": "frozenset",
    "Type": "type",
}

SPECIAL_FORMS: dict[str, str | None] = {
    "Optional": None,  # Optional[X] → X | None
    "Union": " | ",  # Union[X, Y] → X | Y
}

ALL_OLD_NAMES: frozenset[str] = frozenset(list(SIMPLE_RENAMES) + list(SPECIAL_FORMS))

# Files / paths to skip
_SKIP_DIRS: tuple[str, ...] = ("deployer/",)
_SKIP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"_pb2\.py$"),
    re.compile(r"_pb2_grpc\.py$"),
    re.compile(r"_grpc\.py$"),
    re.compile(r"^build/"),
    re.compile(r"\.egg-info/"),
    re.compile(r"__pycache__/"),
]


def _has_future_annotations(tree: ast.Module) -> bool:
    """Return True if *tree* has ``from __future__ import annotations``."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for alias in node.names:
                if alias.name == "annotations":
                    return True
    return False


def _import_stmt_ranges_for_tree(tree: ast.Module, source: str) -> list[tuple[int, int]]:
    """Return (start, end) byte-offset ranges for top-level import statements."""
    ranges: list[tuple[int, int]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            start = _byte_offset(source, node.lineno, 0)
            end = start
            while end < len(source) and source[end] != "\n":
                end += 1
            if end < len(source) and source[end] == "\n":
                end += 1
            ranges.append((start, end))
    return ranges


def _expr_contains_str_literal(expr: ast.expr) -> bool:
    """Return True if *expr* contains a string literal (forward reference).

    Transforming ``Optional["Foo"]`` → ``"Foo" | None`` is unsafe without
    PEP 563 because ``"Foo"`` would be a real string, not a forward ref.
    """
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
    return False


# ---------------------------------------------------------------------------
class Replacement(NamedTuple):
    """A single text replacement to apply."""

    start: int  # byte/char offset
    end: int  # byte/char offset
    text: str  # replacement text


# ---------------------------------------------------------------------------
# AST-based transformation
# ---------------------------------------------------------------------------


class TypingTransformer:
    """Walk an AST, collect old-style typing uses, compute replacements."""

    def __init__(self, source: str, has_future_annotations: bool = False) -> None:
        self.source = source
        # keepends=True so line-length sums include \n or \r\n correctly
        self._lines: list[str] = source.splitlines(keepends=True)
        self.replacements: list[Replacement] = []
        # Track which old names appear in type-annotation position
        self._used_old_names: set[str] = set()
        # When False, skip annotations that contain string forward-references
        self.has_future_annotations = has_future_annotations

    # -- offset helpers ------------------------------------------------------
    def _node_offset(self, node: ast.AST) -> int:
        """Character offset of *node*'s start position (1-based lineno/col)."""
        lineno = getattr(node, "lineno", 1)
        col_offset = getattr(node, "col_offset", 0)
        return self._pos_to_offset(lineno, col_offset)

    def _node_end(self, node: ast.AST) -> int:
        """Character offset just past *node*'s end."""
        end_lineno = getattr(node, "end_lineno", None)
        end_col_offset = getattr(node, "end_col_offset", None)
        if end_lineno is None or end_col_offset is None:
            # Fallback: use start offset + len of unparsed
            return self._node_offset(node) + len(ast.unparse(node))
        return self._pos_to_offset(end_lineno, end_col_offset)

    def _pos_to_offset(self, lineno: int, col_offset: int) -> int:
        """Convert 1-based (lineno, col_offset) to 0-based char offset.

        Uses ``splitlines(keepends=True)`` so each line's length includes
        the actual newline sequence (``\n`` or ``\r\n``).
        """
        offset = 0
        for i in range(lineno - 1):
            offset += len(self._lines[i])  # full line including newline
        return offset + col_offset

    # -- annotation extraction -----------------------------------------------
    def _annotation_node(self, node: ast.AST) -> ast.AST | ast.Constant | None:
        """Return the annotation expression node (or string Constant for PEP 563)."""
        if hasattr(node, "annotation") and node.annotation is not None:
            return node.annotation
        # FunctionDef / AsyncFunctionDef.returns
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            return node.returns
        return None

    def _parse_annotation_str(self, ann_str: str) -> ast.expr | None:
        """Parse a string annotation back into an expression AST.

        Returns None on failure (e.g. forward-reference strings we can't parse).
        """
        try:
            mod = ast.parse(ann_str.strip(), mode="eval")
            if isinstance(mod, ast.Expression):
                return mod.body
        except SyntaxError:
            pass
        return None

    # -- name detection ------------------------------------------------------
    @staticmethod
    def _is_old_name(node: ast.expr | None) -> str | None:
        """If *node* is a Name referencing an old-style typing symbol, return the symbol id."""
        if isinstance(node, ast.Name) and node.id in ALL_OLD_NAMES:
            return node.id
        return None

    def _safe_to_transform(self, expr: ast.expr) -> bool:
        """Skip *expr* if it has string forward-refs without PEP 563."""
        if self.has_future_annotations:
            return True
        return not _expr_contains_str_literal(expr)

    # -- collection pass -----------------------------------------------------
    def collect(self, tree: ast.AST) -> None:
        """Walk *tree* and collect all old-style typing references."""
        for node in ast.walk(tree):
            annotation = self._annotation_node(node)
            if annotation is not None and self._safe_to_transform(annotation):
                self._collect_from_expr(annotation)

        # Also collect bare old-style names used at runtime
        # (e.g. ``isinstance(x, Dict)``) so the import can be cleaned up.
        import_ranges = self._import_stmt_ranges(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name):
                continue
            if node.id not in SIMPLE_RENAMES:
                continue
            # Skip names inside import statements
            pos = self._node_offset(node)
            if any(s <= pos < e for s, e in import_ranges):
                continue
            self._used_old_names.add(node.id)

    def _collect_from_expr(self, expr: ast.expr) -> None:
        """Recursively examine an annotation expression for old-style names."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            # PEP 563 stringified annotation — parse it
            inner = self._parse_annotation_str(expr.value)
            if inner is not None:
                self._collect_from_expr(inner)
            return

        if isinstance(expr, ast.Subscript):
            old_name = self._is_old_name(expr.value)
            if old_name:
                self._used_old_names.add(old_name)
            # Recurse into slice
            self._collect_from_expr(expr.slice)
        elif isinstance(expr, ast.Tuple):
            for elt in expr.elts:
                self._collect_from_expr(elt)
        elif isinstance(expr, ast.BinOp):
            # e.g. X | Y (already modern, skip)
            pass
        elif isinstance(expr, ast.Name):
            # Bare (non-subscripted) old-style name: ``x: Dict`` → ``x: dict``
            if expr.id in ALL_OLD_NAMES:
                self._used_old_names.add(expr.id)
        # Also walk children generically
        for child in ast.iter_child_nodes(expr):
            if isinstance(child, ast.expr):
                self._collect_from_expr(child)

    def _import_stmt_ranges(self, tree: ast.AST) -> list[tuple[int, int]]:
        """Return (start, end) byte-offset ranges for every top-level import."""
        ranges: list[tuple[int, int]] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                start = self._pos_to_offset(node.lineno, 0)
                end = start
                while end < len(self.source) and self.source[end] != "\n":
                    end += 1
                if end < len(self.source) and self.source[end] == "\n":
                    end += 1
                ranges.append((start, end))
        return ranges

    # -- transformation pass -------------------------------------------------
    def transform(self, tree: ast.AST) -> None:
        """Walk *tree* and compute Replacement entries for old-style typing.

        Uses a bottom-up recursive approach: nested types like
        ``Optional[Dict[str, Any]]`` are fully rewritten to
        ``dict[str, Any] | None`` in a single replacement.

        Skips annotations containing string forward-references unless
        ``from __future__ import annotations`` is active (PEP 563).

        Also transforms bare old-style names at runtime
        (e.g. ``isinstance(x, Dict)`` → ``isinstance(x, dict)``).
        """
        # -- annotation pass --
        annotation_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            annotation = self._annotation_node(node)
            if annotation is None:
                continue
            if not self._safe_to_transform(annotation):
                continue
            annotation_ranges.append((self._node_offset(annotation), self._node_end(annotation)))
            self._transform_annotation(annotation)

        # -- runtime bare-name pass --
        import_ranges = self._import_stmt_ranges(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name):
                continue
            if node.id not in SIMPLE_RENAMES:
                continue
            pos = self._node_offset(node)
            # Skip names inside import statements
            if any(s <= pos < e for s, e in import_ranges):
                continue
            # Skip names inside annotations (already handled above)
            if any(s <= pos < e for s, e in annotation_ranges):
                continue
            new_name = SIMPLE_RENAMES[node.id]
            self.replacements.append(Replacement(pos, self._node_end(node), new_name))

    def _transform_annotation(self, expr: ast.expr) -> None:
        """Transform one top-level annotation expression, producing a single Replacement."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            # PEP 563 stringified annotation
            inner = self._parse_annotation_str(expr.value)
            if inner is None:
                return
            new_text = self._transform_to_text(inner)
            if new_text is None:
                return  # no change needed
            start = self._node_offset(expr)
            end = self._node_end(expr)
            quote = self.source[start]  # " or '
            self.replacements.append(Replacement(start, end, f"{quote}{new_text}{quote}"))
            return

        new_text = self._transform_to_text(expr)
        if new_text is None:
            return  # no old-style types found in this annotation

        start = self._node_offset(expr)
        end = self._node_end(expr)
        if ast.unparse(expr) != new_text:
            self.replacements.append(Replacement(start, end, new_text))

    def _transform_to_text(self, expr: ast.expr) -> str | None:
        """Recursively transform *expr* to modern Python text.

        Returns the new text, or **None** if no old-style types were found
        anywhere in the subtree — meaning the expression is already modern.
        """
        changed = False

        if isinstance(expr, ast.Subscript):
            old_name = self._is_old_name(expr.value)
            if old_name is not None:
                changed = True
                inner = self._transform_slice(expr.slice)

                if old_name == "Optional":
                    return f"{inner} | None"

                if old_name == "Union":
                    # Union[X, Y, Z] → X | Y | Z  (the slice is a Tuple)
                    if isinstance(expr.slice, ast.Tuple):
                        parts: list[str] = []
                        for elt in expr.slice.elts:
                            t = self._transform_to_text(elt)
                            parts.append(t if t is not None else ast.unparse(elt))
                        return " | ".join(parts)
                    # Union[X] — single-element, unusual but valid
                    t = self._transform_to_text(expr.slice)
                    return t if t is not None else ast.unparse(expr.slice)

                new_name = SIMPLE_RENAMES[old_name]
                return f"{new_name}[{inner}]"

            # Not an old name at this level — but children might be
            name_text = ast.unparse(expr.value)
            inner = self._transform_slice(expr.slice)
            # Check if inner was actually changed
            orig_inner = _slice_text(expr.slice)
            if inner != orig_inner:
                return f"{name_text}[{inner}]"
            return None

        if isinstance(expr, ast.Tuple):
            parts: list[str] = []
            for elt in expr.elts:
                part = self._transform_to_text(elt)
                if part is not None:
                    changed = True
                    parts.append(part)
                else:
                    parts.append(ast.unparse(elt))
            if changed:
                return ", ".join(parts)
            return None

        if isinstance(expr, ast.BinOp):
            # Already-modern ``X | Y`` — transform both sides recursively
            left = self._transform_to_text(expr.left)
            right = self._transform_to_text(expr.right)
            if left is not None or right is not None:
                return f"{left or ast.unparse(expr.left)} | {right or ast.unparse(expr.right)}"
            return None

        if isinstance(expr, ast.Name):
            # Bare old-style name: ``x: Dict`` → ``x: dict``
            if expr.id in SIMPLE_RENAMES:
                return SIMPLE_RENAMES[expr.id]
            return None

        # Fallback: preserve as-is
        return None

    def _transform_slice(self, slice_expr: ast.expr) -> str:
        """Transform the *inside* of ``[...]`` brackets.

        For ``Dict[K, V]`` / ``Tuple[X, Y]`` the slice is a Tuple whose
        elements need individual transformation.  For ``Optional[X]`` it is
        a single expression.

        Returns the transformed slice text (comma-joined for multi-param,
        or the single expression for single-param).
        """
        if isinstance(slice_expr, ast.Tuple):
            parts: list[str] = []
            for elt in slice_expr.elts:
                t = self._transform_to_text(elt)
                parts.append(t if t is not None else ast.unparse(elt))
            return ", ".join(parts)

        # Single element: Optional[X], List[X], Set[X], etc.
        t = self._transform_to_text(slice_expr)
        return t if t is not None else ast.unparse(slice_expr)

    # -- import cleanup ------------------------------------------------------
    def _collect_import_nodes(self, tree: ast.AST) -> list[tuple[ast.ImportFrom, str, int, int]]:
        """Collect all ``from typing import X`` nodes with their offsets."""
        imports: list[tuple[ast.ImportFrom, str, int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "typing":
                continue
            for alias in node.names:
                start = self._node_offset(alias)
                end = self._node_end(alias)
                imports.append((node, alias.name, start, end))
        return imports

    @staticmethod
    def _import_statement_range(source: str, node: ast.ImportFrom, offset_fn) -> tuple[int, int]:
        """Return (start, end) character offsets covering *node* and its newline.

        Handles single-line and parenthesized multi-line forms.
        """
        line_start = offset_fn(node.lineno, 0)
        line_end = line_start
        while line_end < len(source) and source[line_end] != "\n":
            line_end += 1

        if "(" in source[line_start:line_end]:
            paren_depth = 0
            for i in range(line_start, len(source)):
                ch = source[i]
                if ch == "(":
                    paren_depth += 1
                elif ch == ")":
                    paren_depth -= 1
                    if paren_depth == 0:
                        line_end = i
                        break
            while line_end < len(source) and source[line_end] != "\n":
                line_end += 1

        if line_end < len(source) and source[line_end] == "\n":
            line_end += 1

        return line_start, line_end

    def _remove_import_statement(self, node: ast.ImportFrom) -> None:
        """Remove the entire ``from typing import ...`` statement."""
        line_start, line_end = self._import_statement_range(self.source, node, self._pos_to_offset)
        self.replacements.append(Replacement(line_start, line_end, ""))

    def _rewrite_import_line(self, node: ast.ImportFrom, keep_names: list[str]) -> None:
        """Rewrite the import line keeping only *keep_names*."""
        line_start, line_end = self._import_statement_range(self.source, node, self._pos_to_offset)
        indent = self.source[line_start : line_start + node.col_offset]

        original_text = self.source[line_start:line_end]
        if "(" in original_text:
            inner = "(\n    " + ",\n    ".join(keep_names) + ",\n)"
            new_line = f"{indent}from typing import {inner}\n"
        else:
            new_line = f"{indent}from typing import {', '.join(keep_names)}\n"

        self.replacements.append(Replacement(line_start, line_end, new_line))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _slice_text(expr: ast.expr) -> str:
    """Unparse the inside of ``[...]`` brackets.

    For a Tuple slice (multi-param generics) this returns comma-joined
    elements.  For a single expression this returns its source text.
    """
    if isinstance(expr, ast.Tuple):
        return ", ".join(ast.unparse(elt) for elt in expr.elts)
    return ast.unparse(expr)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def _should_skip(rel_path: str) -> bool:
    return any(rel_path.startswith(d) for d in _SKIP_DIRS) or any(pat.search(rel_path) for pat in _SKIP_PATTERNS)


def process_file(path: Path) -> bool:
    """Check and transform *path* in-place. Returns True if modified."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    if not source.strip():
        return False

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False

    transformer = TypingTransformer(source, has_future_annotations=_has_future_annotations(tree))

    # Phase 1: collect old-style names used in annotations
    transformer.collect(tree)

    if not transformer._used_old_names:
        return False

    # Phase 2: compute expression-level replacements
    transformer.transform(tree)

    if not transformer.replacements:
        return False

    # Phase 3: apply expression replacements to a virtual copy so we can
    #          accurately check whether an old-style name is *still* used
    #          elsewhere (e.g. ``cast(Dict[...])``, ``isinstance(x, Dict)``).
    virtual = source
    for repl in sorted(transformer.replacements, key=lambda r: r.start, reverse=True):
        virtual = virtual[: repl.start] + repl.text + virtual[repl.end :]

    # Phase 4: determine which typing imports are truly obsolete
    # Group obsolete names by import node so we rewrite each import line ONCE.
    obsolete_by_node: dict[int, set[str]] = {}  # id(node) → {names to remove}
    typing_imports = transformer._collect_import_nodes(tree)

    for import_node, name, start, end in typing_imports:
        if name not in transformer._used_old_names:
            continue
        # Use AST-based check to avoid false positives from docstrings/comments
        # (e.g. "Optional" mentioned in prose).  Inverted: obsolete when the
        # name does NOT appear in real code outside the import itself.
        if not _ast_has_name(virtual, name, exclude_ranges=[(start, end)]):
            obsolete_by_node.setdefault(id(import_node), set()).add(name)

    # Phase 5: compute import-cleanup replacements (using *original* offsets,
    #          which are still valid because import lines precede annotations).
    for import_node, name, start, end in typing_imports:
        obsolete_set = obsolete_by_node.get(id(import_node))
        if obsolete_set is None:
            continue
        # Only process each node once
        if name not in obsolete_set:
            continue
        # Rewrite the line without ALL obsolete names for this node
        keep_names = [alias.name for alias in import_node.names if alias.name not in obsolete_set]
        if not keep_names:
            # Remove the entire import statement
            transformer._remove_import_statement(import_node)
        else:
            transformer._rewrite_import_line(import_node, keep_names)
        # Clear so we don't process this node again
        del obsolete_by_node[id(import_node)]

    if not transformer.replacements:
        return False

    # Phase 6: apply all replacements from end to start
    all_replacements = sorted(transformer.replacements, key=lambda r: r.start, reverse=True)
    result = source
    for repl in all_replacements:
        result = result[: repl.start] + repl.text + result[repl.end :]

    path.write_text(result, encoding="utf-8")
    return True


def _count_word_occurrences(text: str, word: str, exclude_ranges: list[tuple[int, int]] | None = None) -> int:
    """Count whole-word occurrences of *word* in *text* (regex fallback)."""
    pattern = re.compile(rf"\b{re.escape(word)}\b")
    count = 0
    for m in pattern.finditer(text):
        pos = m.start()
        if exclude_ranges and any(s <= pos < e for s, e in exclude_ranges):
            continue
        count += 1
    return count


def _byte_offset(text: str, lineno: int, col_offset: int) -> int:
    """Convert 1-based (lineno, col_offset) to 0-based byte offset in *text*."""
    offset = 0
    for i, line in enumerate(text.splitlines(keepends=True), start=1):
        if i >= lineno:
            break
        offset += len(line)
    return offset + col_offset


def _ast_has_name(text: str, name: str, exclude_ranges: list[tuple[int, int]]) -> bool:
    """Return True if *name* appears as a Python identifier outside *exclude_ranges*.

    Uses AST to avoid false positives from comments / docstrings.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _count_word_occurrences(text, name, exclude_ranges) > 0

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            pos = _byte_offset(text, node.lineno, node.col_offset)
            if not any(s <= pos < e for s, e in exclude_ranges):
                return True
    return False


# ---------------------------------------------------------------------------
def _detect_issues(source: str) -> list[str]:
    """Find old-style typing names used in *annotation positions* (AST-based).

    Mirrors the ``collect()`` logic in TypingTransformer so that ``--check``
    and auto-fix agree on exactly what will be changed.  Comments, docstrings,
    and runtime calls like ``cast(Dict[...])`` are intentionally not flagged.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    used: set[str] = set()

    def _collect(expr: ast.expr) -> None:
        """Recursively walk an annotation expression for old-style names."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            # PEP 563 stringified annotation
            try:
                mod = ast.parse(expr.value.strip(), mode="eval")
                if isinstance(mod, ast.Expression):
                    _collect(mod.body)
            except SyntaxError:
                pass
            return

        if isinstance(expr, ast.Subscript):
            if isinstance(expr.value, ast.Name) and expr.value.id in ALL_OLD_NAMES:
                used.add(expr.value.id)
            _collect(expr.slice)
        elif isinstance(expr, ast.Tuple):
            for elt in expr.elts:
                _collect(elt)
        elif isinstance(expr, ast.BinOp):
            _collect(expr.left)
            _collect(expr.right)
        elif isinstance(expr, ast.Name):
            # Bare old-style name: ``x: Dict``
            if expr.id in ALL_OLD_NAMES:
                used.add(expr.id)
        # Walk any nested expression children
        for child in ast.iter_child_nodes(expr):
            if isinstance(child, ast.expr):
                _collect(child)

    has_future = _has_future_annotations(tree)

    for node in ast.walk(tree):
        annotation: ast.expr | None = None
        if hasattr(node, "annotation") and node.annotation is not None:
            annotation = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotation = node.returns

        if annotation is None:
            continue
        # Skip annotations with string forward-references unless PEP 563 is active
        if not has_future and _expr_contains_str_literal(annotation):
            continue
        _collect(annotation)

    # Also scan for bare old-style names used at runtime
    # (e.g. ``isinstance(x, Dict)`` → ``isinstance(x, dict)``)
    import_ranges = _import_stmt_ranges_for_tree(tree, source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name):
            continue
        if node.id not in SIMPLE_RENAMES:
            continue
        pos = _byte_offset(source, node.lineno, node.col_offset)
        if any(s <= pos < e for s, e in import_ranges):
            continue
        used.add(node.id)

    return sorted(used)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Modernize old-style Python typing annotations.",
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
    issue_files: list[tuple[str, list[str]]] = []

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
            continue

        if not source.strip():
            continue

        issues = _detect_issues(source)
        if not issues:
            continue

        if args.check:
            issue_files.append((rel, issues))
        else:
            if process_file(path):
                print(f"check_modern_typing: fixed {rel} — old-style: {', '.join(sorted(set(issues)))}")
                modified_count += 1

    if args.check and issue_files:
        for rel, issues in issue_files:
            print(f"{rel}: old-style typing — {', '.join(sorted(set(issues)))}")
        print(f"check_modern_typing: {len(issue_files)} file(s) need fixes (re-run without --check to auto-fix)")
        return 1

    if modified_count:
        print(f"check_modern_typing: fixed {modified_count} file(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
