# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from __future__ import annotations

import builtins
import sys
from types import ModuleType
from typing import Optional

from ..config import CONFIG
from ..constants import ALG_AES_GCM_128
from ..crypto_adapter import (
    secure_d2h_decrypt_after_async_copy,
    secure_d2h_encrypt_for_async_copy,
)
from ..patch_utils import is_npu_tensor
from ..runtime import guard, log, remember_patch, wrap_errors

PATCH_NAME = "async_output_d2h"

_TARGET_MODULE = "vllm.v1.worker.gpu_model_runner"
_TARGET_CLASS = "AsyncGPUModelRunnerOutput"
_PATCH_KEY = "vllm.AsyncGPUModelRunnerOutput.__init__"

_ORIGINAL_IMPORT = None
_IMPORT_HOOK_INSTALLED = False
_PATCHED = False


def _debug_enabled() -> bool:
    return bool(getattr(CONFIG, "debug", False))


def _is_enabled() -> bool:
    if hasattr(CONFIG, "enable_async_output_d2h"):
        return bool(getattr(CONFIG, "enable_async_output_d2h"))
    if hasattr(CONFIG, "patch_async_output_d2h"):
        return bool(getattr(CONFIG, "patch_async_output_d2h"))
    return True


def _get_loaded_target_module() -> Optional[ModuleType]:
    mod = sys.modules.get(_TARGET_MODULE)
    return mod if isinstance(mod, ModuleType) else None


def _call_original(
    original,
    self,
    model_runner_output,
    sampled_token_ids,
    logprobs_tensors,
    invalid_req_indices,
    async_output_copy_stream,
    vocab_size,
    *extra_args,
    **extra_kwargs,
):
    return original(
        self,
        model_runner_output,
        sampled_token_ids,
        logprobs_tensors,
        invalid_req_indices,
        async_output_copy_stream,
        vocab_size,
        *extra_args,
        **extra_kwargs,
    )


def _synchronize_async_output_copy_stream(
    async_output_copy_stream,
) -> None:
    if async_output_copy_stream is None:
        return

    sync = getattr(async_output_copy_stream, "synchronize", None)
    if sync is None:
        if _debug_enabled():
            log(
                "async_output_d2h: async_output_copy_stream "
                "has no synchronize(): "
                f"type={type(async_output_copy_stream)}"
            )
        return

    sync()


def _synchronize_d2h_device(ctx) -> None:
    import torch

    device = torch.device(f"npu:{ctx.local_device_id}")

    try:
        torch.npu.synchronize(device)
    except TypeError:
        previous_device = None

        try:
            previous_device = torch.npu.current_device()
        except Exception:
            previous_device = None

        try:
            torch.npu.set_device(device)
            torch.npu.synchronize()
        finally:
            if previous_device is not None:
                try:
                    torch.npu.set_device(previous_device)
                except Exception as exc:
                    log(f"async_output_d2h failed to restore device{previous_device}:{exc!r}")


def _make_copy_stream_wait_encrypt(
    ctx,
    encrypted_tensor,
    async_output_copy_stream,
) -> bool:
    if async_output_copy_stream is None:
        if _debug_enabled():
            log("async_output_d2h: async_output_copy_stream is None, cannot wait encrypt event")
        return False

    event = getattr(ctx, "encrypt_event", None)
    if event is None:
        if _debug_enabled():
            log("async_output_d2h: ctx.encrypt_event is None, cannot wait encrypt event")
        return False

    try:
        wait_event = getattr(
            async_output_copy_stream,
            "wait_event",
            None,
        )
        if wait_event is None:
            if _debug_enabled():
                log(
                    "async_output_d2h: async_output_copy_stream "
                    "has no wait_event(): "
                    f"type={type(async_output_copy_stream)}"
                )
            return False

        wait_event(event)

        record_stream = getattr(
            encrypted_tensor,
            "record_stream",
            None,
        )
        if record_stream is not None:
            record_stream(async_output_copy_stream)

        if _debug_enabled():
            log(
                "async_output_d2h: copy stream wait encrypt event "
                "success: "
                f"event={event}, "
                f"stream={async_output_copy_stream}"
            )

        return True

    except Exception as exc:
        if _debug_enabled():
            log(f"async_output_d2h: copy stream wait encrypt event failed: {repr(exc)}")
        return False


def _patch_loaded_module(mod: ModuleType) -> bool:
    global _PATCHED

    if _PATCHED:
        return True

    cls = getattr(mod, _TARGET_CLASS, None)
    if cls is None:
        return False

    original = getattr(cls, "__init__", None)
    if original is None:
        return False

    if not remember_patch(_PATCH_KEY, original):
        _PATCHED = True
        return True

    @wrap_errors(PATCH_NAME, original)
    def patched(
        self,
        model_runner_output,
        sampled_token_ids,
        logprobs_tensors,
        invalid_req_indices,
        async_output_copy_stream,
        vocab_size,
        *extra_args,
        **extra_kwargs,
    ):
        with guard(PATCH_NAME) as active:
            if not active or not is_npu_tensor(sampled_token_ids):
                return _call_original(
                    original,
                    self,
                    model_runner_output,
                    sampled_token_ids,
                    logprobs_tensors,
                    invalid_req_indices,
                    async_output_copy_stream,
                    vocab_size,
                    *extra_args,
                    **extra_kwargs,
                )

            original_called = False

            try:
                encrypted_sampled_token_ids, ctx = secure_d2h_encrypt_for_async_copy(sampled_token_ids)

                ok = _make_copy_stream_wait_encrypt(
                    ctx,
                    encrypted_sampled_token_ids,
                    async_output_copy_stream,
                )

                if not ok:
                    import torch

                    device = encrypted_sampled_token_ids.device

                    try:
                        torch.npu.synchronize(device)
                    except TypeError:
                        torch.npu.synchronize()

                    if _debug_enabled():
                        log("async_output_d2h: fallback torch.npu.synchronize after encrypt")

                ret = _call_original(
                    original,
                    self,
                    model_runner_output,
                    encrypted_sampled_token_ids,
                    logprobs_tensors,
                    invalid_req_indices,
                    async_output_copy_stream,
                    vocab_size,
                    *extra_args,
                    **extra_kwargs,
                )
                original_called = True

                if not hasattr(self, "sampled_token_ids_cpu"):
                    raise RuntimeError("AsyncGPUModelRunnerOutput has no sampled_token_ids_cpu after original __init__")

                _synchronize_async_output_copy_stream(async_output_copy_stream)

                if ctx.alg_id == ALG_AES_GCM_128:
                    _synchronize_d2h_device(ctx)

                self.sampled_token_ids_cpu = secure_d2h_decrypt_after_async_copy(
                    self.sampled_token_ids_cpu,
                    ctx,
                )

                return ret

            except Exception as exc:
                if getattr(CONFIG, "strict", False):
                    raise

                log(f"async_output_d2h failed: {repr(exc)}")

                if original_called:
                    raise

                return _call_original(
                    original,
                    self,
                    model_runner_output,
                    sampled_token_ids,
                    logprobs_tensors,
                    invalid_req_indices,
                    async_output_copy_stream,
                    vocab_size,
                    *extra_args,
                    **extra_kwargs,
                )


def _try_patch_if_loaded() -> bool:
    mod = _get_loaded_target_module()
    return False if mod is None else _patch_loaded_module(mod)


def _restore_import_hook_if_done() -> None:
    global _ORIGINAL_IMPORT
    global _IMPORT_HOOK_INSTALLED

    if not _IMPORT_HOOK_INSTALLED:
        return

    if _ORIGINAL_IMPORT is not None and builtins.__import__ is _secure_patch_import_hook:
        builtins.__import__ = _ORIGINAL_IMPORT

    _ORIGINAL_IMPORT = None
    _IMPORT_HOOK_INSTALLED = False


def _secure_patch_import_hook(
    name,
    globals_vars=None,
    locals_vars=None,
    fromlist=(),
    level=0,
):
    mod = _ORIGINAL_IMPORT(
        name,
        globals_vars,
        locals_vars,
        fromlist,
        level,
    )

    try:
        if not _PATCHED and _try_patch_if_loaded():
            _restore_import_hook_if_done()
    except Exception as exc:
        if getattr(CONFIG, "strict", False):
            raise

        log(f"async_output_d2h lazy patch failed: {repr(exc)}")

    return mod


def _install_import_hook() -> bool:
    global _ORIGINAL_IMPORT
    global _IMPORT_HOOK_INSTALLED

    if _IMPORT_HOOK_INSTALLED:
        return True

    _ORIGINAL_IMPORT = builtins.__import__
    builtins.__import__ = _secure_patch_import_hook
    _IMPORT_HOOK_INSTALLED = True

    if _debug_enabled():
        log(f"registered lazy patch hook for {_TARGET_MODULE}.{_TARGET_CLASS}")

    return True


def install_async_output_d2h_patch() -> bool:
    if not _is_enabled():
        log("async_output_d2h patch disabled by config")
        return False

    if _try_patch_if_loaded():
        return True

    return _install_import_hook()
