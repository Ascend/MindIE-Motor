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
from pathlib import Path

import pytest

_DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"


def _annotation_uses_pep604_union(node: ast.expr | None) -> bool:
    if node is None:
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
            return True
    return False


def _uses_pep604_union(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    class _Visitor(ast.NodeVisitor):
        found = False

        def _check_args(self, arguments: ast.arguments) -> None:
            for arg in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ):
                if _annotation_uses_pep604_union(arg.annotation):
                    self.found = True
            if arguments.vararg and _annotation_uses_pep604_union(arguments.vararg.annotation):
                self.found = True
            if arguments.kwarg and _annotation_uses_pep604_union(arguments.kwarg.annotation):
                self.found = True

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if _annotation_uses_pep604_union(node.returns):
                self.found = True
            self._check_args(node.args)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if _annotation_uses_pep604_union(node.annotation):
                self.found = True
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.found


def _deployer_py_files() -> list[Path]:
    return sorted(path for path in _DEPLOYER_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


@pytest.mark.parametrize("module_path", _deployer_py_files(), ids=lambda p: p.relative_to(_DEPLOYER_ROOT).as_posix())
def test_pep604_annotations_require_future_import(module_path: Path) -> None:
    text = module_path.read_text(encoding="utf-8")
    if not _uses_pep604_union(text):
        pytest.skip("no PEP 604 union annotations")
    assert "from __future__ import annotations" in text, (
        f"{module_path.relative_to(_DEPLOYER_ROOT)} uses X | Y annotations but lacks from __future__ import annotations"
    )


def test_update_host_ipc_from_env_importable() -> None:
    from lib.generator.engine import update_host_ipc_from_env

    update_host_ipc_from_env(None)
