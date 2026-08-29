# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Static Render API contracts shared by tokenization, routing, and response assembly."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderApiSpec:
    api: str
    request_payload_field: str
    generate_payload_field: str
    output_budget_fields: tuple[str, ...]
    supports_prompt_batch: bool

    @property
    def render_path(self) -> str:
        return f"/{self.api}/render"

    @property
    def derender_path(self) -> str:
        return f"/{self.api}/derender"


_API_SPECS = {
    "v1/chat/completions": RenderApiSpec(
        api="v1/chat/completions",
        request_payload_field="chat_request",
        generate_payload_field="generate_response",
        output_budget_fields=("max_completion_tokens", "max_tokens"),
        supports_prompt_batch=False,
    ),
    "v1/completions": RenderApiSpec(
        api="v1/completions",
        request_payload_field="completion_request",
        generate_payload_field="generate_responses",
        output_budget_fields=("max_tokens",),
        supports_prompt_batch=True,
    ),
}


def get_render_api_spec(api: str) -> RenderApiSpec | None:
    """Resolve a supported API without treating unknown routes as Chat."""
    return _API_SPECS.get(api.strip("/"))
