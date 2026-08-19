# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
import threading
from pathlib import Path

from motor.common.resources.instance import Instance, PDRole
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.resources.endpoint import Endpoint
from motor.coordinator.domain import InstanceProvider
from motor.coordinator.domain.block_offset_translator import attach_block_offsets
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy, WorkloadLedgerMixin
from motor.config.coordinator import (
    AIGW_D_MAX_SEQLEN,
    AIGW_P_MAX_SEQLEN,
    CoordinatorConfig,
    CONTEXT_BUDGET_ON,
    KV_AFFINITY_MODE_LOAD_GATED,
    KV_AFFINITY_MODE_UNIFIED,
)
from motor.common.logger import get_logger
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.api_client.conductor_api_client import (
    ConductorApiClient,
    TENANT_ID,
    conductor_instance_id,
)
from motor.coordinator.scheduler.policy.utils import (
    preprocess_input,
    preprocess_messages_for_dsv4,
    preprocess_messages_for_standard,
)

__all__ = ["KvCacheAffinityPolicy", "TokenizerManager", "adapt_context_budget"]

logger = get_logger(__name__)

# Endpoints kept by the load-gated mode when kv_affinity.load_gate_topn is left unset (0).
_DEFAULT_LOAD_GATE_TOPN = 2


def adapt_context_budget(
    req_info: RequestInfo,
    config: CoordinatorConfig,
) -> None:
    """Clamp the active output-token limit before routing, independent of scheduler type."""
    if config.context_budget_mode != CONTEXT_BUDGET_ON:
        return

    req_data = req_info.req_data
    parameters = (
        (OpenAIField.MAX_COMPLETION_TOKENS, OpenAIField.MAX_TOKENS)
        if OpenAIField.MESSAGES in req_data
        else (OpenAIField.MAX_TOKENS,)
    )
    for parameter in parameters:
        requested_tokens = req_data.get(parameter)
        if isinstance(requested_tokens, int) and not isinstance(requested_tokens, bool) and requested_tokens > 0:
            break
    else:
        return

    aigw_model = config.get_aigw_models() or {}
    context_limits = [
        value
        for value in (
            aigw_model.get(AIGW_P_MAX_SEQLEN),
            aigw_model.get(AIGW_D_MAX_SEQLEN),
        )
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ]
    if not context_limits:
        return

    token_ids = KvCacheAffinityPolicy._ensure_token_ids(req_info)
    if not token_ids:
        return

    max_model_len = min(context_limits)
    available_tokens = max_model_len - len(token_ids)
    if available_tokens < 1:
        return

    effective_tokens = min(requested_tokens, available_tokens)
    req_data[parameter] = effective_tokens
    if effective_tokens < requested_tokens:
        logger.info(
            "Context budget clamped req_id=%s parameter=%s requested=%d effective=%d prompt_tokens=%d max_model_len=%d",
            req_info.req_id,
            parameter,
            requested_tokens,
            effective_tokens,
            len(token_ids),
            max_model_len,
        )


class KvCacheAffinityPolicy(WorkloadLedgerMixin, BaseSchedulingPolicy):
    """
    KvCache Affinity Scheduler Policy implementation.
    Selects instances and endpoints in a kvcache-affinity fashion.
    """

    def __init__(self, instance_provider: InstanceProvider):
        super().__init__(instance_provider=instance_provider)

        logger.info("KvCacheAffinityPolicy started.")

    @staticmethod
    def select_endpoint_candidates_from_list(
        instances: list[Instance],
        req_info: RequestInfo,
        mode: str = KV_AFFINITY_MODE_UNIFIED,
        overlap_credit: float = 1.0,
        prefill_load_scale: float = 1.0,
        load_weight: float = 1.0,
        load_gate_topn: int = 0,
        w_npu: float = 1.0,
        w_cpu: float = 1.0,
        w_disk: float = 0.0,
        top_k: int = 1,
    ) -> list[tuple[Instance, Endpoint, float]] | None:
        """
        Rank prefill (instance, endpoint) candidates by KV-cache prefix affinity, best first.

        Returns up to ``top_k`` ``(instance, endpoint, score)`` tuples ordered best-first (lower
        score = better), or ``None`` to let the caller fall back. The worker proposes this ranked
        set to the scheduler; the scheduler may re-pick among them by its authoritative (fresh)
        workload ledger -- so spreading a burst across the top candidates is the scheduler's job,
        not a client-local in-flight overlay.

        Two modes, chosen explicitly by ``mode``:

        * ``"unified"`` (default): a single score that fuses affinity and live load.
          Every reported endpoint is scored by
          ``prefill_load_scale * max(0, isl - overlap_credit * matched_tokens)
          + load_weight * workload_score`` (all terms in token units); the lowest scores win. With
          ``load_weight == 0`` this degenerates to pure prefix-affinity (longest prefix wins).
        * ``"load_gated"``: a two-stage "load first, affinity second" selection. Only the
          ``load_gate_topn`` least-loaded endpoints survive (defaulting to 2 when unset), and among
          those the longest cached prefixes rank first. A *hard* load bound -- affinity can never
          pull the request onto an endpoint outside the least-loaded set.

        Fast path: a prompt shorter than one KV block can never produce a cached-prefix hit (the
        conductor indexer hashes only whole blocks), so the blocking conductor round-trip is
        skipped and the ranking runs against an all-zero match map -- identical to what an all-zero
        conductor response would yield, but without the network call.

        :param instances: candidate prefill instances.
        :param req_info: request whose prompt/messages drive the conductor prefix query.
        :param mode: ``"unified"`` (default) or ``"load_gated"``; unknown values fall back to
            ``"unified"``.
        :param overlap_credit: how much a cached prefix discounts prefill work (default 1.0).
        :param prefill_load_scale: weight of the (discounted) prefill cost (default 1.0).
        :param load_weight: weight of the endpoint's current workload in the unified score
            (default 1.0); 0 makes the unified score affinity-only.
        :param load_gate_topn: number of least-loaded endpoints kept by the ``"load_gated"`` mode
            before the affinity ranking; 0 (default) falls back to 2. Ignored by ``"unified"``.
        :param w_npu: weight for exclusive NPU matched blocks (default 1.0).
        :param w_cpu: weight for exclusive CPU matched blocks (default 1.0).
        :param w_disk: weight for exclusive Disk matched blocks (default 0.0).
        :param top_k: maximum number of ranked candidates to return (>=1).
        :returns: best-first ``[(instance, endpoint, score), ...]`` or ``None`` to fall back.
        """
        encoded_ids = KvCacheAffinityPolicy._ensure_token_ids(req_info)

        block_size = KvCacheAffinityPolicy._conductor_block_size()
        if block_size > 0 and len(encoded_ids) < block_size:
            # Sub-block prompt: the indexer hashes only whole blocks, so a query can only ever
            # report a zero match. Skip the blocking HTTP round-trip and rank against an all-zero
            # match map -- identical to what the conductor would return for this prompt, but
            # without the network cost.
            tenant = {conductor_instance_id(inst): {"DP": {}} for inst in instances}
        else:
            rsp = ConductorApiClient.query_conductor(instances, encoded_ids)
            tenant = rsp.get(TENANT_ID, None)
            if tenant is None:
                logger.warning(
                    "kv_cache_affinity: conductor query returned no tenant data (tenant_id=%s, instances=%d)",
                    TENANT_ID,
                    len(instances),
                )
                return None

        if mode == KV_AFFINITY_MODE_LOAD_GATED:
            topn = load_gate_topn if (load_gate_topn and load_gate_topn > 0) else _DEFAULT_LOAD_GATE_TOPN
            return KvCacheAffinityPolicy._select_load_gated(
                instances,
                tenant,
                len(encoded_ids),
                overlap_credit,
                topn,
                top_k,
                req_info=req_info,
                w_npu=w_npu,
                w_cpu=w_cpu,
                w_disk=w_disk,
                block_size=block_size,
            )

        # "unified" (default); unknown modes fall through here too.
        return KvCacheAffinityPolicy._select_with_load(
            instances,
            tenant,
            len(encoded_ids),
            overlap_credit,
            prefill_load_scale,
            load_weight,
            top_k,
            req_info=req_info,
            w_npu=w_npu,
            w_cpu=w_cpu,
            w_disk=w_disk,
            block_size=block_size,
        )

    @staticmethod
    def select_endpoint_from_list(
        instances: list[Instance],
        req_info: RequestInfo,
        mode: str = KV_AFFINITY_MODE_UNIFIED,
        overlap_credit: float = 1.0,
        prefill_load_scale: float = 1.0,
        load_weight: float = 1.0,
        load_gate_topn: int = 0,
        w_npu: float = 1.0,
        w_cpu: float = 1.0,
        w_disk: float = 0.0,
    ) -> tuple[Instance, Endpoint] | None:
        """
        Single-result convenience wrapper over :meth:`select_endpoint_candidates_from_list`.

        Returns the best ``(instance, endpoint)`` or ``None``. Prefer the candidates method when
        the scheduler should be allowed to re-pick among the top candidates by fresh load.
        """
        ranked = KvCacheAffinityPolicy.select_endpoint_candidates_from_list(
            instances,
            req_info,
            mode=mode,
            overlap_credit=overlap_credit,
            prefill_load_scale=prefill_load_scale,
            load_weight=load_weight,
            load_gate_topn=load_gate_topn,
            w_npu=w_npu,
            w_cpu=w_cpu,
            w_disk=w_disk,
            top_k=1,
        )
        if not ranked:
            return None
        instance, endpoint, _score = ranked[0]
        return (instance, endpoint)

    @staticmethod
    def _ensure_token_ids(req_info: RequestInfo) -> list[int]:
        """
        Tokenize the prompt once and cache it on ``req_info.token_ids`` for reuse.

        The same token ids feed context budgeting, the conductor prefix query, prefill cost,
        and workload accounting. Returns the cached list when already present so a request is
        tokenized at most once.
        """
        cached = getattr(req_info, "token_ids", None)
        if isinstance(cached, list):
            return cached
        encoded_ids: list[int] = []
        messages = req_info.req_data.get(OpenAIField.MESSAGES, None)
        tools = req_info.req_data.get(OpenAIField.TOOLS, None)
        try:
            if messages is not None:
                tokenizer_manager = TokenizerManager()
                encoded_ids = tokenizer_manager.apply_chat_template(messages, tools, req_data=req_info.req_data)
                attach_block_offsets(req_info, messages, tools, tokenizer=tokenizer_manager.tokenizer)
            else:
                prompt = req_info.req_data.get(OpenAIField.PROMPT, None)
                if isinstance(prompt, str):
                    encoded_ids = TokenizerManager().encode(prompt)
                elif (
                    isinstance(prompt, list)
                    and prompt
                    and all(isinstance(token_id, int) and not isinstance(token_id, bool) for token_id in prompt)
                ):
                    encoded_ids = prompt.copy()
                elif prompt is not None:
                    logger.info(
                        "kv_cache_affinity: unsupported prompt type %s; falling back to load_balance",
                        type(prompt).__name__,
                    )
        except Exception as e:
            logger.warning("kv_cache_affinity tokenization failed; falling back to load_balance: %s", e)
            encoded_ids = []
        if not isinstance(encoded_ids, list) or any(
            not isinstance(token_id, int) or isinstance(token_id, bool) for token_id in encoded_ids
        ):
            logger.warning("kv_cache_affinity tokenizer returned invalid token ids; falling back to load_balance")
            encoded_ids = []
        try:
            req_info.token_ids = encoded_ids
        except Exception as e:  # pragma: no cover - req_info may be immutable in some callers
            logger.debug("Could not cache token_ids on req_info: %s", e)
        logger.debug(
            "kv_affinity tokenize ok: msgs=%d tools=%d encoded_ids=%d",
            len(messages) if isinstance(messages, list) else 0,
            len(tools) if isinstance(tools, list) else 0,
            len(encoded_ids),
        )
        return encoded_ids

    @staticmethod
    def _conductor_block_size() -> int:
        """
        Configured KV block size (tokens per block); 0 when unknown so the fast path stays off.

        Used to skip the conductor round-trip for prompts shorter than one block (which can never
        hit a cached prefix). Defensive on purpose: any config hiccup returns 0, which disables the
        skip (no behavior change) rather than raising on the selection hot path.
        """
        try:
            bs = int(ConductorApiClient.coordinator_config.scheduler_config.kv_conductor_config.block_size)
            return bs if bs > 0 else 0
        except Exception as e:  # pragma: no cover - config shape guard
            logger.debug("Could not read conductor block_size: %s", e)
            return 0

    @staticmethod
    def _weighted_matched_tokens(
        matched_raw: object,
        block_size: int,
        w_npu: float,
        w_cpu: float,
        w_disk: float,
    ) -> int:
        """
        Score a DP match for affinity.

        Prefer exclusive ``*_blocks`` × tier weights × ``block_size``. Fall back to conductor
        ``matched_tokens`` (or a plain int) when blocks are unavailable.
        """
        if isinstance(matched_raw, dict):
            npu = matched_raw.get("npu_blocks")
            cpu = matched_raw.get("cpu_blocks")
            disk = matched_raw.get("disk_blocks")
            if npu is not None or cpu is not None or disk is not None:
                if block_size > 0:
                    effective = float(npu or 0) * w_npu + float(cpu or 0) * w_cpu + float(disk or 0) * w_disk
                    return int(round(effective * block_size))
            return int(matched_raw.get("matched_tokens", 0) or 0)
        return int(matched_raw or 0)

    @staticmethod
    def _collect_load_candidates(
        instances: list[Instance],
        tenant: dict,
        isl: int,
        overlap_credit: float,
        w_npu: float = 1.0,
        w_cpu: float = 1.0,
        w_disk: float = 0.0,
        block_size: int = 0,
    ) -> tuple[list[tuple[float, int, float, Instance, Endpoint]], bool]:
        """
        Build the per-endpoint scoring tuples shared by the load-aware selection modes.

        Each candidate is ``(load_cost, matched_tokens, prefill_cost, instance, endpoint)`` where
        ``load_cost`` is the SHM-reported live workload and ``matched_tokens`` is the
        tier-weighted affinity match capped at the prompt. Returns
        ``(candidates, any_instance)``; ``any_instance`` distinguishes "conductor reported nothing
        for our instances" (fall back) from "reported, but no endpoints".
        """
        candidates: list[tuple[float, int, float, Instance, Endpoint]] = []
        any_instance = False
        for instance in instances:
            instance_data = tenant.get(conductor_instance_id(instance), None)
            if instance_data is None:
                continue
            any_instance = True
            dp_map = instance_data.get("DP", {})
            # get_all_endpoints() is the canonical accessor: it flattens the per-DP map and
            # already excludes headless endpoints / respects enable_multi_endpoints.
            for ep in instance.get_all_endpoints():
                matched_raw = dp_map.get(f"{ep.id}", 0)
                matched = KvCacheAffinityPolicy._weighted_matched_tokens(matched_raw, block_size, w_npu, w_cpu, w_disk)
                # Cap at the prompt length as a safety bound, since a matched prefix
                # cannot be longer than the prompt itself.
                matched_tokens = min(matched, isl) if isl > 0 else 0
                prefill_cost = max(0.0, isl - overlap_credit * matched_tokens)
                load_cost = ep.workload.calculate_workload_score(PDRole.ROLE_P)
                candidates.append((load_cost, matched_tokens, prefill_cost, instance, ep))
        return candidates, any_instance

    @staticmethod
    def _stash_affinity_debug(
        req_info: RequestInfo | None,
        raw: list[tuple[float, int, float, Instance, Endpoint]],
        with_prefill: bool = False,
    ) -> None:
        """
        Cache per-endpoint ``(matched_tokens, load_cost, prefill_cost)`` on ``req_info``.

        Two consumers:
        * the worker's final allocation log, which reports the KV-affinity prefix hit and load of
          the endpoint the scheduler actually committed (may differ from the worker's top-1 after
          the scheduler's fresh-ledger re-pick);
        * unified-mode :meth:`AsyncSchedulerClient.select_and_allocate`, which forwards every
          endpoint's affinity-discounted ``prefill_cost`` to the scheduler so it can re-rank all of
          them by its own fresh load (``prefill_load_scale * prefill_cost + load_weight * load``) --
          a global selection with no fixed top-k.

        ``prefill_cost`` is stored only when ``with_prefill`` is set; it is None otherwise (e.g.
        load_gated, whose hard load bound must not be relaxed into a soft unified score on the
        scheduler). Best-effort: never fail selection over a debug cache.
        """
        if req_info is None:
            return
        try:
            req_info.kv_affinity_debug = {
                (instance.id, ep.id): (matched_tokens, load_cost, prefill_cost if with_prefill else None)
                for (load_cost, matched_tokens, prefill_cost, instance, ep) in raw
            }
        except Exception as e:  # pragma: no cover - req_info may be immutable in some callers
            logger.debug("Could not cache kv_affinity_debug on req_info: %s", e)

    @staticmethod
    def _select_with_load(
        instances: list[Instance],
        tenant: dict,
        isl: int,
        overlap_credit: float,
        prefill_load_scale: float,
        load_weight: float,
        top_k: int = 1,
        req_info: RequestInfo | None = None,
        w_npu: float = 1.0,
        w_cpu: float = 1.0,
        w_disk: float = 0.0,
        block_size: int = 0,
    ) -> list[tuple[Instance, Endpoint, float]] | None:
        """
        Unified cost: score every reported endpoint by affinity-discounted prefill
        work plus live workload, and return the ``top_k`` lowest-scoring (best) candidates. An
        endpoint with no cached prefix can still rank high when it is far less loaded, which avoids
        herding onto a single hot-prefix endpoint. With ``load_weight == 0`` the score is
        affinity-only (longest prefix wins).
        """
        raw, any_instance = KvCacheAffinityPolicy._collect_load_candidates(
            instances,
            tenant,
            isl,
            overlap_credit,
            w_npu=w_npu,
            w_cpu=w_cpu,
            w_disk=w_disk,
            block_size=block_size,
        )
        if not any_instance:
            logger.warning("kv_cache_affinity(load-aware): no instance data")
            return None
        if not raw:
            logger.warning("kv_cache_affinity(load-aware): no endpoint selected")
            return None

        # Each candidate: (score, instance, endpoint, matched_tokens); lower score is better.
        candidates = [
            (prefill_load_scale * prefill_cost + load_weight * load_cost, instance, ep, matched_tokens)
            for (load_cost, matched_tokens, prefill_cost, instance, ep) in raw
        ]
        ranked = sorted(candidates, key=lambda c: c[0])[: max(1, top_k)]
        top_score, top_inst, top_ep, top_matched = ranked[0]
        # DEBUG, not INFO: this is only the worker's *proposal*. The request's real destination is
        # decided by the scheduler's authoritative re-pick and logged once at INFO ("scheduled ...")
        # in AsyncSchedulerClient.select_and_allocate. Emitting this at INFO misleads load analysis.
        logger.debug(
            "select_endpoint(load-aware): role=%s %s-%s matched:%s score:%.2f (top%d of %d)",
            top_inst.role,
            top_inst.id,
            top_ep.id,
            top_matched,
            top_score,
            len(ranked),
            len(candidates),
        )
        KvCacheAffinityPolicy._stash_affinity_debug(req_info, raw, with_prefill=True)
        return [(inst, ep, score) for (score, inst, ep, _matched) in ranked]

    @staticmethod
    def _select_load_gated(
        instances: list[Instance],
        tenant: dict,
        isl: int,
        overlap_credit: float,
        load_gate_topn: int,
        top_k: int = 1,
        req_info: RequestInfo | None = None,
        w_npu: float = 1.0,
        w_cpu: float = 1.0,
        w_disk: float = 0.0,
        block_size: int = 0,
    ) -> list[tuple[Instance, Endpoint, float]] | None:
        """
        Two-stage "load first, affinity second" ranking: keep only the ``load_gate_topn``
        least-loaded endpoints, then rank them by longest cached prefix (tie -> lighter load),
        returning the best ``top_k``.

        This gives a *hard* load bound (the choice can never escape the least-loaded set) while
        still exploiting KV-cache affinity as the tie-break inside that set.
        """
        raw, any_instance = KvCacheAffinityPolicy._collect_load_candidates(
            instances,
            tenant,
            isl,
            overlap_credit,
            w_npu=w_npu,
            w_cpu=w_cpu,
            w_disk=w_disk,
            block_size=block_size,
        )
        if not any_instance:
            logger.warning("kv_cache_affinity(load-gated): no instance data")
            return None
        if not raw:
            logger.warning("kv_cache_affinity(load-gated): no endpoint selected")
            return None

        # Candidate is (load_cost, matched_tokens, prefill_cost, instance, endpoint).
        # Stage 1: keep the N least-loaded endpoints.
        topn = max(1, load_gate_topn)
        gated = sorted(raw, key=lambda c: c[0])[:topn]
        # Stage 2: rank the least-loaded by longest cached prefix; tie -> lighter load.
        ranked = sorted(gated, key=lambda c: (-c[1], c[0]))[: max(1, top_k)]
        top_load, top_matched, _prefill, top_inst, top_ep = ranked[0]
        # DEBUG, not INFO: worker proposal only; see _select_with_load / "scheduled ..." for the
        # authoritative destination the scheduler committed.
        logger.debug(
            "select_endpoint(load-gated): role=%s %s-%s matched:%s load:%.2f (top%d of %d gated, %d total)",
            top_inst.role,
            top_inst.id,
            top_ep.id,
            top_matched,
            top_load,
            len(ranked),
            topn,
            len(raw),
        )
        KvCacheAffinityPolicy._stash_affinity_debug(req_info, raw)
        return [(inst, ep, load_cost) for (load_cost, _m, _p, inst, ep) in ranked]

    def _select_instance(self, _: PDRole = None) -> Instance | None:
        """
        Select an instance with the least workload.
        """
        return None

    def _select_endpoint(self, _: Instance) -> Endpoint | None:
        """
        Select an endpoint with the least workload from the given instance.
        """
        return None

    def select_instance_and_endpoint_from_list(
        self,
        instances: list[Instance],
        role: PDRole | None = None,
        req_info: RequestInfo | None = None,
    ):
        """Select within a compatible subset, using affinity for prefill and load for other roles."""
        if role == PDRole.ROLE_P and req_info is not None:
            selected = KvCacheAffinityPolicy.select_endpoint_from_list(instances, req_info)
            if selected is not None:
                return selected
        from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy

        return LoadBalancePolicy.select_endpoint_from_list(instances, role)


class TokenizerManager(ThreadSafeSingleton):
    """Tokenizer shared directly by KV affinity and context-budget routing."""

    def __init__(self, config: CoordinatorConfig | None = None):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self.config_lock = threading.RLock()
        if config is None:
            config = CoordinatorConfig()

        self.endpoint = config.tracer_config.endpoint
        self.tokenizer = None
        self._is_dsv4 = False
        scheduler_config = getattr(config, "scheduler_config", None)
        kv_config = getattr(scheduler_config, "kv_conductor_config", None) if scheduler_config else None
        if kv_config is None:
            kv_config = getattr(config, "prefill_kv_event_config", None)
        scheduler_type = getattr(scheduler_config, "scheduler_type", None) if scheduler_config else None
        scheduler_value = getattr(scheduler_type, "value", scheduler_type)
        needs_tokenizer = bool(
            (kv_config and getattr(kv_config, "conductor_service", ""))
            or scheduler_value == "kv_cache_affinity"
            or config.context_budget_mode == CONTEXT_BUDGET_ON
        )
        if not needs_tokenizer:
            logger.info("KV affinity and context budget are disabled. disable TokenizerManager!")
            return

        model_path = getattr(kv_config, "model_path", "") if kv_config else ""
        if model_path:
            os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"
            engine_type = str(getattr(kv_config, "engine_type", "vllm") or "vllm").strip().lower()
            if engine_type == "vllm" and self._is_deepseek_v4_model(model_path):
                from vllm.tokenizers.deepseek_v4 import DeepseekV4Tokenizer

                self.tokenizer = DeepseekV4Tokenizer.from_pretrained(model_path, trust_remote_code=True)
                self._is_dsv4 = True
            else:
                from transformers import AutoTokenizer

                self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        self.openai_standard = os.environ.get("OPENAI_STANDARD", "STANDARD")
        logger.info("TokenizerManager init.(model_path:%s, is_dsv4:%s)", model_path, self._is_dsv4)

    def apply_chat_template(self, messages: list, tools: list | None = None, req_data: dict | None = None) -> list[int]:
        if self.tokenizer is None:
            return []
        try:
            if self._is_dsv4:
                return self._apply_chat_template_dsv4(messages, tools, req_data)
            if self.openai_standard != "STANDARD":
                return self._apply_chat_template_with_preprocess(messages, tools, req_data)
            return self._apply_chat_template_standard(messages, tools, req_data)
        except Exception as exc:
            if self._is_dsv4:
                logger.error("kv_affinity dsv4 tokenize failed; returning []: %s", exc)
                return []
            logger.warning("kv_affinity primary tokenize path failed: %s; trying fallback", exc)
            return self._safe_fallback_encode(messages, tools, req_data)

    def encode(self, prompt: str) -> list[int]:
        return [] if self.tokenizer is None else self.tokenizer.encode(prompt)

    @staticmethod
    def _read_model_config_dict(model_path: str) -> dict | None:
        try:
            with open(Path(model_path) / "config.json", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError) as exc:
            logger.debug("Could not read config.json from %s: %s", model_path, exc)
            return None

    @staticmethod
    def _is_deepseek_v4_model(model_path: str) -> bool:
        config_dict = TokenizerManager._read_model_config_dict(model_path)
        if not config_dict:
            return False
        return config_dict.get("model_type") == "deepseek_v4" or "DeepseekV4ForCausalLM" in (
            config_dict.get("architectures") or []
        )

    @staticmethod
    def _build_dsv4_chat_template_kwargs(req_data: dict | None) -> dict:
        kwargs: dict = {"tokenize": True, "drop_thinking": True}
        if not req_data:
            return kwargs
        reasoning_effort = req_data.get("reasoning_effort")
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        chat_template_kwargs = req_data.get("chat_template_kwargs") or {}
        if isinstance(chat_template_kwargs, dict):
            kwargs.update(chat_template_kwargs)
        if reasoning_effort is not None and "enable_thinking" not in kwargs:
            kwargs["enable_thinking"] = reasoning_effort != "none"
        return kwargs

    @staticmethod
    def _build_standard_chat_template_kwargs(req_data: dict | None, *, tokenize: bool) -> dict:
        kwargs: dict = {"add_generation_prompt": True, "tokenize": tokenize}
        if tokenize:
            kwargs["return_dict"] = False
        if not req_data:
            return kwargs
        if isinstance(req_data.get("add_generation_prompt"), bool):
            kwargs["add_generation_prompt"] = req_data["add_generation_prompt"]
        if req_data.get("continue_final_message"):
            kwargs["continue_final_message"] = True
            kwargs["add_generation_prompt"] = False
        if req_data.get("documents") is not None:
            kwargs["documents"] = req_data["documents"]
        template_kwargs = req_data.get("chat_template_kwargs") or {}
        if isinstance(template_kwargs, dict):
            reserved = {
                "tokenize",
                "return_dict",
                "conversation",
                "tools",
                "add_generation_prompt",
                "continue_final_message",
            }
            kwargs.update({key: value for key, value in template_kwargs.items() if key not in reserved})
        reasoning_effort = req_data.get("reasoning_effort")
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs.setdefault("enable_thinking", reasoning_effort != "none")
        thinking = req_data.get("thinking")
        if isinstance(thinking, dict) and "enable_thinking" not in kwargs:
            if thinking.get("type") == "enabled":
                kwargs["enable_thinking"] = True
            elif thinking.get("type") == "disabled":
                kwargs["enable_thinking"] = False
        return kwargs

    def _apply_chat_template_dsv4(self, messages: list, tools: list | None, req_data: dict | None) -> list[int]:
        messages, tools = preprocess_messages_for_dsv4(messages, tools)
        result = self.tokenizer.apply_chat_template(
            messages, tools=tools, **self._build_dsv4_chat_template_kwargs(req_data)
        )
        return result if isinstance(result, list) else self.tokenizer.encode(result, add_special_tokens=False)

    def _apply_chat_template_standard(self, messages: list, tools: list | None, req_data: dict | None) -> list[int]:
        return self.tokenizer.apply_chat_template(
            conversation=preprocess_messages_for_standard(messages),
            tools=tools,
            **self._build_standard_chat_template_kwargs(req_data, tokenize=True),
        )

    def _apply_chat_template_with_preprocess(
        self, messages: list, tools: list | None, req_data: dict | None
    ) -> list[int]:
        messages, tools = preprocess_input(messages, tools)
        prompt = self.tokenizer.apply_chat_template(
            conversation=messages,
            tools=tools,
            **self._build_standard_chat_template_kwargs(req_data, tokenize=False),
        )
        return self.tokenizer.encode(prompt)

    def _safe_fallback_encode(self, messages: list, tools: list | None, req_data: dict | None) -> list[int]:
        try:
            if self.openai_standard == "STANDARD":
                return self._apply_chat_template_with_preprocess(messages, tools, req_data)
            return self._apply_chat_template_standard(messages, tools, req_data)
        except Exception as exc:
            logger.error("kv_affinity tokenize failed on both primary and fallback paths; returning []: %s", exc)
            return []
