# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Validated cache identity shared by request ingress and Conductor queries."""

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr, field_validator


def _identity_string(value: str) -> str:
    """Identity components use nonempty UTF-8 strings with u32 byte lengths."""
    if not value or len(value.encode("utf-8")) > 0xFFFFFFFF:
        raise ValueError("identity strings must be nonempty and fit a u32 UTF-8 length")
    return value


IdentityString = Annotated[StrictStr, AfterValidator(_identity_string)]


class BaseAdapter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["base"]


class LoraAdapter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["lora"]
    name: IdentityString


class CacheIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    adapter: Annotated[BaseAdapter | LoraAdapter, Field(discriminator="kind")]
    cache_salt: IdentityString | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value != 1:
            raise ValueError("cache identity schema_version must be integer 1")
        return value


def resolve_request_cache_identity(
    request_data: dict[str, Any], request_model_map: dict[str, dict[str, str]]
) -> CacheIdentity | None:
    """Resolve configured model names without changing the engine request body."""
    model = request_data.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError("model must be a nonempty string")
    salt = request_data.get("cache_salt")
    if salt is not None:
        if not isinstance(salt, str):
            raise ValueError("cache_salt must be null or a nonempty string")
        _identity_string(salt)
    adapter = request_model_map.get(model)
    if adapter is None:
        return None
    return CacheIdentity(schema_version=1, adapter=adapter, cache_salt=salt)
