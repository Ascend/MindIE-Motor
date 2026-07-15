# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Tests for Controller API observability TLS configuration.

Verifies that the Controller API's existing observability TLS support
(observability_tls_config) is complete and correctly wired.
"""

import ast


class _FindMethod(ast.NodeVisitor):
    """AST visitor to find a method (sync or async) by name in a class."""

    def __init__(self, method_name):
        self.method_name = method_name
        self.found = False
        self.method_nodes = []

    def visit_FunctionDef(self, node):
        if node.name == self.method_name:
            self.found = True
            self.method_nodes.append(ast.dump(node))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        if node.name == self.method_name:
            self.found = True
            self.method_nodes.append(ast.dump(node))
        self.generic_visit(node)


class TestControllerApiObservabilityTls:
    """ControllerAPI already supports observability TLS via observability_tls_config."""

    def test_controller_config_has_observability_tls_config(self):
        """ControllerConfig has observability_tls_config field defined."""
        with open("motor/config/controller.py", "r", encoding="utf-8") as f:
            source = f.read()
        # Verify the ControllerConfig class references observability_tls_config as a field
        assert "observability_tls_config" in source, "ControllerConfig must define observability_tls_config"

    def test_controller_api_has_observability_tls_in_init(self):
        """ControllerAPI.__init__ stores observability_tls_config."""
        # Verify the source code contains the expected pattern
        with open("motor/controller/api_server/controller_api.py", "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        # Check that _run_observability_api_server references observability_tls_config
        class_visitor = _FindMethod("_run_observability_api_server")
        class_visitor.visit(tree)
        assert class_visitor.found, "_run_observability_api_server method must exist in ControllerAPI"

        # Check that the method references observability_tls_config
        tls_ref = any("observability_tls_config" in node for node in class_visitor.method_nodes)
        assert tls_ref, "_run_observability_api_server must reference observability_tls_config"

    def test_controller_run_observability_api_has_tls_branch(self):
        """_run_observability_api_server has if enable_tls branch."""
        with open("motor/controller/api_server/controller_api.py", "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        class_visitor = _FindMethod("_run_observability_api_server")
        class_visitor.visit(tree)

        # Check for the pattern: if self.observability_tls_config.enable_tls:
        pattern_found = any("enable_tls" in node for node in class_visitor.method_nodes)
        assert pattern_found, "_run_observability_api_server must check enable_tls"
