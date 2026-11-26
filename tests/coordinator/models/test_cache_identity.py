# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import msgspec
import pytest
from pydantic import ValidationError

from motor.coordinator.models.cache_identity import CacheIdentity, resolve_request_cache_identity


def test_omitted_and_null_salt_resolve_to_the_same_identity():
    fields = {"schema_version": 1, "adapter": {"kind": "base"}}
    assert CacheIdentity.model_validate(fields) == CacheIdentity.model_validate({**fields, "cache_salt": None})


@pytest.mark.parametrize("adapter", [{"kind": "base"}, {"kind": "lora", "name": "适配器@1"}])
@pytest.mark.parametrize("salt", [None, " ", "session-1"])
def test_cache_identity_json_and_msgpack_share_the_same_fields(adapter, salt):
    payload = {"schema_version": 1, "adapter": adapter, "cache_salt": salt}
    identity = CacheIdentity.model_validate(payload)
    assert CacheIdentity.model_validate_json(identity.model_dump_json()) == identity
    assert (
        CacheIdentity.model_validate(msgspec.msgpack.decode(msgspec.msgpack.encode(identity.model_dump()))) == identity
    )
    assert identity.model_dump() == payload


@pytest.mark.parametrize(
    "fields",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"cache_salt": ""},
        {"cache_salt": 1},
        {"cache_salt": "\ud800"},
        {"adapter": {"kind": "unknown"}},
        {"adapter": {"kind": "base", "name": "x"}},
        {"adapter": {"kind": "lora", "name": ""}},
        {"adapter": {"kind": "lora", "name": 1}},
    ],
)
def test_cache_identity_rejects_ambiguous_or_invalid_wire_values(fields):
    with pytest.raises(ValidationError):
        CacheIdentity.model_validate({"schema_version": 1, "adapter": {"kind": "base"}, "cache_salt": None, **fields})


def test_request_identity_does_not_default_unknown_model_to_base_or_rewrite_request():
    mapping = {"base": {"kind": "base"}, "lora": {"kind": "lora", "name": "lora"}}
    request = {"model": "lora", "cache_salt": "session"}
    identity = resolve_request_cache_identity(request, mapping)
    assert identity.adapter.name == "lora"
    assert identity.cache_salt == "session"
    assert request == {"model": "lora", "cache_salt": "session"}
    assert resolve_request_cache_identity({"model": "base"}, mapping).cache_salt is None
    assert resolve_request_cache_identity({"model": "unknown"}, mapping) is None
    assert resolve_request_cache_identity({}, mapping) is None
