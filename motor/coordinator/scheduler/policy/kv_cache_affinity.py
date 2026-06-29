# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import threading

from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint, Workload, WorkloadAction
from motor.coordinator.domain import InstanceProvider
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.config.coordinator import (
    CoordinatorConfig,
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
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.coordinator.scheduler.policy.utils import preprocess_input


logger = get_logger(__name__)

# Endpoints kept by the load-gated mode when kv_affinity_load_gate_topn is left unset (0).
_DEFAULT_LOAD_GATE_TOPN = 2


class KvCacheAffinityPolicy(BaseSchedulingPolicy):
    """
    KvCache Affinity Scheduler Policy implementation.
    Selects instances and endpoints in a kvcache-affinity fashion.
    """

    def __init__(self, instance_provider: InstanceProvider):
        super().__init__(instance_provider=instance_provider)
        self._instance_provider = instance_provider

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

        The same token ids feed (a) the conductor prefix query, (b) ``isl`` for the prefill cost,
        and (c) ``calculate_demand_workload`` so the committed prefill load is in real tokens
        rather than the byte-length heuristic. Returns the cached list when already present so a
        request is tokenized at most once.
        """
        cached = getattr(req_info, "token_ids", None)
        if isinstance(cached, list):
            return cached
        encoded_ids: list[int] = []
        messages = req_info.req_data.get(OpenAIField.MESSAGES, None)
        tools = req_info.req_data.get(OpenAIField.TOOLS, None)
        if messages is not None:
            encoded_ids = TokenizerManager().apply_chat_template(messages, tools)
        else:
            prompt = req_info.req_data.get(OpenAIField.PROMPT, None)
            if prompt is not None:
                encoded_ids = TokenizerManager().encode(prompt)
        try:
            req_info.token_ids = encoded_ids
        except Exception as e:  # pragma: no cover - req_info may be immutable in some callers
            logger.debug("Could not cache token_ids on req_info: %s", e)
        # Visibility for the validated invariant: tools, when present, MUST inflate
        # the encoded token sequence. Operators can grep this line to verify
        # function-call requests are being tokenised correctly.
        logger.debug(
            "kv_affinity tokenize ok: msgs=%d tools=%d encoded_ids=%d",
            len(messages or []),
            len(tools or []),
            len(encoded_ids or []),
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
    def _collect_load_candidates(
        instances: list[Instance],
        tenant: dict,
        isl: int,
        overlap_credit: float,
    ) -> tuple[list[tuple[float, int, float, Instance, Endpoint]], bool]:
        """
        Build the per-endpoint scoring tuples shared by the load-aware selection modes.

        Each candidate is ``(load_cost, matched_tokens, prefill_cost, instance, endpoint)`` where
        ``load_cost`` is the SHM-reported live workload and ``matched_tokens`` is the
        conductor-reported cached prefix length capped at the prompt. Returns
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
                # Conductor reports per-DP match data. Since the multi-medium scoring
                # revision (DpScoring struct), the value is a dict with a "matched_tokens"
                # key; older conductors returned a plain int. Handle both.
                if isinstance(matched_raw, dict):
                    matched = matched_raw.get("matched_tokens", 0)
                else:
                    matched = matched_raw
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
    ) -> list[tuple[Instance, Endpoint, float]] | None:
        """
        Unified cost: score every reported endpoint by affinity-discounted prefill
        work plus live workload, and return the ``top_k`` lowest-scoring (best) candidates. An
        endpoint with no cached prefix can still rank high when it is far less loaded, which avoids
        herding onto a single hot-prefix endpoint. With ``load_weight == 0`` the score is
        affinity-only (longest prefix wins).
        """
        raw, any_instance = KvCacheAffinityPolicy._collect_load_candidates(instances, tenant, isl, overlap_credit)
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
    ) -> list[tuple[Instance, Endpoint, float]] | None:
        """
        Two-stage "load first, affinity second" ranking: keep only the ``load_gate_topn``
        least-loaded endpoints, then rank them by longest cached prefix (tie -> lighter load),
        returning the best ``top_k``.

        This gives a *hard* load bound (the choice can never escape the least-loaded set) while
        still exploiting KV-cache affinity as the tie-break inside that set.
        """
        raw, any_instance = KvCacheAffinityPolicy._collect_load_candidates(instances, tenant, isl, overlap_credit)
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

    async def update_workload(
        self,
        instance_id: int,
        endpoint_id: int,
        req_id: str,
        workload_action: WorkloadAction,
        workload_change: Workload,
    ) -> bool:
        """
        Update workload after KV-affinity selection.

        KV-affinity decides where prefill should land, but the central workload ledger is still
        needed by decode/fallback load-balance paths and by worker SHM synchronization.
        """
        if hasattr(self._instance_provider, "update_instance_workload"):
            await self._instance_provider.update_instance_workload(instance_id, endpoint_id, workload_change)
        else:
            raise RuntimeError("InstanceProvider must support update_instance_workload for KvCacheAffinityPolicy")

        if req_id:
            logger.debug(
                f"Request {req_id} updated workload: instance_id={instance_id}, "
                f"endpoint_id={endpoint_id}, action={workload_action.value}, "
                f"change={workload_change}"
            )
        else:
            logger.debug(
                f"Updated workload: instance_id={instance_id}, "
                f"endpoint_id={endpoint_id}, action={workload_action.value}, "
                f"change={workload_change}"
            )
        return True


class TokenizerManager(ThreadSafeSingleton):
    """
    Tracer Manager class, Singleton class
    """

    def __init__(self, config: CoordinatorConfig | None = None):
        """TracerManager init"""
        # If the instance manager is already initialized, return.
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self.config_lock = threading.RLock()

        if config is None:
            config = CoordinatorConfig()

        self.endpoint = config.tracer_config.endpoint

        self.tokenizer = None

        kv_reg = config.scheduler_config.kv_conductor_config
        if kv_reg.conductor_service == "":
            logger.info("conductor_service is empty. disable TokenizerManager!")
            return

        model_path = kv_reg.model_path
        if model_path:
            os.environ['TORCH_DEVICE_BACKEND_AUTOLOAD'] = '0'
            from transformers import AutoTokenizer

            self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

        logger.info(f"TokenizerManager init.(model_path:{model_path})")

        self.openai_standard = os.environ.get("OPENAI_STANDARD", "STANDARD")

    def apply_chat_template(self, messages: list, tools: list | None = None) -> list[int]:
        """Render messages (and optional tools) into token ids for KV-cache affinity.

        The output token sequence is the *same* one vLLM/SGLang sees during
        actual inference, so conductor's ``longest_matched`` truly reflects the
        cluster's KV-cache distribution. ``tools`` MUST be forwarded on every
        path - dropping it silently was the bug fixed in this revision.
        """
        if self.tokenizer is None:
            return []

        try:
            if self.openai_standard != "STANDARD":
                return self._apply_chat_template_with_preprocess(messages, tools)
            return self._apply_chat_template_standard(messages, tools)
        except Exception as e:
            logger.warning(
                "kv_affinity primary tokenize path failed: %s; trying tools-aware fallback (msgs=%d, tools=%d)",
                e,
                len(messages or []),
                len(tools or []),
            )
            return self._safe_fallback_encode(messages, tools)

    def encode(self, prompt: str) -> list[int]:
        """
        When the inference API /v1/completions is called,
        this method is used for encoding.
        """
        if self.tokenizer is None:
            return []
        result = self.tokenizer.encode(prompt)
        return result

    def _apply_chat_template_standard(self, messages: list, tools: list | None = None) -> list[int]:
        """Standard OpenAI-compatible model path.

        Calls the model tokenizer's jinja chat-template directly with ``tools``,
        ``add_generation_prompt=True`` and ``tokenize=True`` so the resulting
        token ids are byte-equivalent to what vLLM/SGLang prefill receives.
        """
        return self.tokenizer.apply_chat_template(
            conversation=messages,
            tools=tools,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=False,
        )

    def _apply_chat_template_with_preprocess(self, messages: list, tools: list | None = None) -> list[int]:
        """Non-standard model path: normalise messages/tools then encode the
        rendered prompt string. Kept for models whose chat-template cannot be
        directly invoked with ``tokenize=True`` (e.g. require argument coercion
        or reordering done by ``preprocess_input``).
        """
        messages_copy, tools_copy = preprocess_input(messages, tools)

        prompt = self.tokenizer.apply_chat_template(
            conversation=messages_copy,
            tools=tools_copy,
            tokenize=False,
        )
        return self.tokenizer.encode(prompt)

    def _safe_fallback_encode(self, messages: list, tools: list | None = None) -> list[int]:
        """Last-resort tokenize that NEVER drops ``tools``.

        Tries the tools-aware standard call once more; if that also fails,
        returns ``[]`` so the affinity candidate path can fall back to load balance.
        Returning a partially-correct token list (e.g. messages without tools)
        would silently mislead conductor's longest_matched and is far worse
        than failing closed.
        """
        try:
            return self._apply_chat_template_standard(messages, tools)
        except Exception as e:
            logger.error(
                "kv_affinity tokenize failed on both primary and fallback paths; "
                "returning [] so scheduler falls back to LoadBalance. "
                "msgs=%d tools=%d err=%s",
                len(messages or []),
                len(tools or []),
                e,
            )
            return []
