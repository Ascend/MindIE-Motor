# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Coordinator HTTP routing layer: dispatch entry and strategy implementations."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from motor.coordinator.router.dispatch import handle_request

__all__ = ["handle_request"]


def __getattr__(name: str):
    """Load the HTTP entry lazily so adapter imports do not initialize the full domain graph."""
    if name == "handle_request":
        from motor.coordinator.router.dispatch import handle_request

        return handle_request
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
