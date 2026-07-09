# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -*- coding: utf-8 -*-
from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from typing import Optional, Tuple

import torch

from .config import CONFIG
from .constants import (
    ALG_AES_CTR_128,
    ALG_AES_GCM_128,
    DEVICE_OP_KEY_TYPE,
    GCM_TAG_BYTES,
    HOST_CTR_DECRYPT,
    HOST_CTR_ENCRYPT,
    HOST_GCM_DECRYPT,
    HOST_GCM_ENCRYPT,
    KEY_TYPE_D2H,
    KEY_TYPE_H2D,
    MODE_DEVICE_DECRYPT,
    MODE_DEVICE_ENCRYPT,
    MODE_LOAD_KEY,
)
from .device_mapping import (
    global_device_id_from_local,
    kms_device_id_from_local,
    op_device_id_from_local,
)
from .key_manager import get_key_manager
from .kms_protocol import KeyContext
from .op_backend import create_crypto_op_config, load_host_function, _torch_npu_crypto
from .patch_utils import (
    byte_view,
    device_id_from_device,
    device_id_from_tensor,
    normalize_device,
    restore_from_byte_view,
)
from .runtime import log

_HOST_CTR = None
_HOST_GCM = None


@dataclass(frozen=True)
class SecureD2HContext:
    key: KeyContext
    iv: bytes
    alg_id: int
    local_device_id: int
    global_device_id: int
    kms_device_id: int
    orig_dtype: torch.dtype
    orig_shape: Tuple[int, ...]
    num_bytes: int
    tag_cpu: Optional[torch.Tensor] = None
    encrypt_event: object | None = None
    encrypt_stream: object | None = None
    copy_event: object | None = None


def _debug_enabled() -> bool:
    return bool(getattr(CONFIG, "debug", False))


def _sync_npu(device: torch.device | int | None = None) -> None:
    if device is None:
        torch.npu.current_stream().synchronize()
        return

    if isinstance(device, int):
        device = torch.device(f"npu:{device}")

    torch.npu.current_stream(device).synchronize()


def _status_to_int(status) -> int:
    if status is None:
        return 0
    if isinstance(status, int):
        return status
    if isinstance(status, torch.Tensor):
        return 0 if status.numel() == 0 else int(status.detach().cpu().reshape(-1)[0].item())
    if isinstance(status, (tuple, list)) and status:
        return _status_to_int(status[0])
    return int(status)


def _check_status(status, name: str) -> None:
    value = _status_to_int(status)
    if value != 0:
        raise RuntimeError(f"{name} failed, status={value}")


def _bytes_to_u8_tensor(data: bytes) -> torch.Tensor:
    return torch.tensor(list(data), dtype=torch.uint8).contiguous() if data else torch.empty(0, dtype=torch.uint8)


def _bytes_to_i16_tensor_le(data: bytes) -> torch.Tensor:
    if len(data) % 2 != 0:
        raise RuntimeError(f"int16 tensor requires even byte length, got={len(data)}")
    if not data:
        return torch.empty(0, dtype=torch.int16)
    return torch.tensor(struct.unpack("<" + "h" * (len(data) // 2), data), dtype=torch.int16).contiguous()


def _host_ctr():
    global _HOST_CTR
    if _HOST_CTR is None:
        _HOST_CTR = load_host_function(CONFIG.host_ctr_module, CONFIG.host_ctr_function)
    return _HOST_CTR


def _host_gcm():
    global _HOST_GCM
    if _HOST_GCM is None:
        _HOST_GCM = load_host_function(CONFIG.host_gcm_module, CONFIG.host_gcm_function)
    return _HOST_GCM


def _ensure_alg_supported(alg_id: int) -> None:
    if int(alg_id) not in (ALG_AES_CTR_128, ALG_AES_GCM_128):
        raise RuntimeError(f"unsupported fast path alg_id={alg_id}")


def _device_ids(local_device_id: int) -> tuple[int, int, int]:
    return (
        global_device_id_from_local(local_device_id),
        kms_device_id_from_local(local_device_id),
        op_device_id_from_local(local_device_id),
    )


def _host_ctr_crypt(x_cpu_u8: torch.Tensor, key: KeyContext, iv: bytes, mode: int) -> torch.Tensor:
    out = _host_ctr()(
        x_cpu_u8.contiguous(),
        _bytes_to_i16_tensor_le(iv),
        _bytes_to_i16_tensor_le(key.key_bytes),
        torch.tensor([mode], dtype=torch.int16),
    )
    if not isinstance(out, torch.Tensor):
        raise RuntimeError(f"host CTR returned {type(out)}")
    return out.contiguous()


def _host_gcm_crypt(
    x_cpu_u8: torch.Tensor,
    key: KeyContext,
    iv: bytes,
    mode: int,
    tag_in: Optional[torch.Tensor] = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    if len(iv) != int(CONFIG.iv_bytes):
        raise RuntimeError(f"host GCM expected iv_bytes={CONFIG.iv_bytes}, got={len(iv)}")
    if len(key.key_bytes) != int(key.key_len):
        raise RuntimeError(f"invalid key bytes: key_len={key.key_len}, actual={len(key.key_bytes)}")

    x = x_cpu_u8.contiguous()
    iv_i16 = _bytes_to_i16_tensor_le(iv)
    key_i16 = _bytes_to_i16_tensor_le(key.key_bytes)
    mode_i16 = torch.tensor([mode], dtype=torch.int16)
    aad = torch.empty(0, dtype=torch.uint8)
    tag_arg = torch.empty(0, dtype=torch.uint8) if tag_in is None else tag_in.contiguous()

    if tag_in is not None and tag_arg.numel() != GCM_TAG_BYTES:
        raise RuntimeError(f"host GCM expected tag_bytes={GCM_TAG_BYTES}, got={tag_arg.numel()}")

    if _debug_enabled():
        log(
            "HOST_GCM args: "
            f"mode={mode}, x.shape={tuple(x.shape)}, "
            f"iv_i16.shape={tuple(iv_i16.shape)}, key_i16.shape={tuple(key_i16.shape)}, "
            f"tag.shape={tuple(tag_arg.shape)}, key_id={key.key_id}, key_type={key.key_type}"
        )

    out, tag = _host_gcm()(x, iv_i16, key_i16, mode_i16, aad, tag_arg)

    if not isinstance(out, torch.Tensor):
        raise RuntimeError(f"host GCM returned out={type(out)}")
    if not isinstance(tag, torch.Tensor):
        raise RuntimeError(f"host GCM returned tag={type(tag)}")

    return out.contiguous(), tag.contiguous()


def _host_gcm_encrypt(x_cpu_u8: torch.Tensor, key: KeyContext, iv: bytes) -> tuple[torch.Tensor, torch.Tensor]:
    return _host_gcm_crypt(x_cpu_u8, key, iv, HOST_GCM_ENCRYPT, None)


def _host_gcm_decrypt(cipher_cpu_u8: torch.Tensor, tag_cpu: torch.Tensor, key: KeyContext, iv: bytes) -> torch.Tensor:
    out, _ = _host_gcm_crypt(cipher_cpu_u8, key, iv, HOST_GCM_DECRYPT, tag_cpu)
    return out


def _make_op_config(
    *,
    mode: int,
    key: KeyContext,
    op_device_id: int,
    device: torch.device,
) -> torch.Tensor:
    return create_crypto_op_config(
        mode=mode,
        alg_type=key.alg_id,
        key_type=DEVICE_OP_KEY_TYPE,
        key_id=key.key_id,
        device_id=op_device_id,
        device=device,
    )


def _ensure_device_key_loaded(
    *,
    key: KeyContext,
    device: torch.device,
    device_id: int,
) -> torch.Tensor:
    manager = get_key_manager()
    local_device_id = int(device_id)
    global_device_id, kms_device_id, op_device_id = _device_ids(local_device_id)

    cached = manager.get_key_addr(device_id=local_device_id, key=key)
    if cached is not None:
        if _debug_enabled():
            log(
                "LOAD_KEY cache hit: "
                f"local_device={local_device_id}, global_device={global_device_id}, "
                f"kms_device={kms_device_id}, alg={key.alg_id}, "
                f"type={key.key_type}, id={key.key_id}"
            )
        return cached

    with manager.get_key_addr_lock(device_id=local_device_id, key=key):
        cached = manager.get_key_addr(device_id=local_device_id, key=key)
        if cached is not None:
            if _debug_enabled():
                log(
                    "LOAD_KEY cache hit after wait: "
                    f"local_device={local_device_id}, global_device={global_device_id}, "
                    f"kms_device={kms_device_id}, alg={key.alg_id}, "
                    f"type={key.key_type}, id={key.key_id}"
                )
            return cached

        crypto = _torch_npu_crypto()
        key_addr_tensor = torch.zeros(key.key_len, dtype=torch.uint8, device=device)
        iv = torch.zeros(CONFIG.iv_bytes, dtype=torch.uint8, device=device)
        op_config = _make_op_config(
            mode=MODE_LOAD_KEY,
            key=key,
            op_device_id=op_device_id,
            device=device,
        )

        if _debug_enabled():
            log(
                "LOAD_KEY args: "
                f"local_device={local_device_id}, global_device={global_device_id}, "
                f"kms_device={kms_device_id}, op_device={op_device_id}, "
                f"alg={key.alg_id}, real_type={key.key_type}, "
                f"op_type={DEVICE_OP_KEY_TYPE}, key_id={key.key_id}, "
                f"op_config={op_config.detach().cpu().tolist()}"
            )

        if key.alg_id == ALG_AES_GCM_128:
            dummy_plain = torch.empty(16, dtype=torch.uint8, device=device)
            dummy_cipher = torch.empty(16, dtype=torch.uint8, device=device)
            tag = torch.zeros(GCM_TAG_BYTES, dtype=torch.uint8, device=device)
            status = crypto(key_addr_tensor, dummy_plain, dummy_cipher, iv, op_config, tag, None)
        else:
            dummy_plain = torch.empty(1, dtype=torch.uint8, device=device)
            dummy_cipher = torch.empty(1, dtype=torch.uint8, device=device)
            status = crypto(key_addr_tensor, dummy_plain, dummy_cipher, iv, op_config, None, None)

        _check_status(status, "LOAD_KEY")
        _sync_npu(device)

        manager.put_key_addr(
            device_id=local_device_id,
            key=key,
            key_addr_tensor=key_addr_tensor,
        )

        if _debug_enabled():
            log(
                "LOAD_KEY success: "
                f"local_device={local_device_id}, global_device={global_device_id}, "
                f"kms_device={kms_device_id}, alg={key.alg_id}, "
                f"real_type={key.key_type}, id={key.key_id}"
            )

        return key_addr_tensor


def _device_crypt_to(
    x_dev_u8: torch.Tensor,
    out_dev_u8: torch.Tensor,
    *,
    key: KeyContext,
    key_addr_tensor: torch.Tensor,
    iv: bytes,
    device: torch.device,
    local_device_id: int,
    mode: int,
    name: str,
    tag_cpu: Optional[torch.Tensor] = None,
) -> Optional[torch.Tensor]:
    global_device_id, kms_device_id, op_device_id = _device_ids(local_device_id)

    iv_dev = _bytes_to_u8_tensor(iv).to(device).contiguous()
    op_config = _make_op_config(
        mode=mode,
        key=key,
        op_device_id=op_device_id,
        device=device,
    )

    if key.alg_id == ALG_AES_GCM_128:
        tag_arg = (
            torch.zeros(GCM_TAG_BYTES, dtype=torch.uint8, device=device)
            if tag_cpu is None
            else tag_cpu.to(device).contiguous()
        )
    else:
        tag_arg = None

    if _debug_enabled():
        log(
            f"{name}: "
            f"local_device={local_device_id}, global_device={global_device_id}, "
            f"kms_device={kms_device_id}, op_device={op_device_id}, "
            f"key_id={key.key_id}, real_key_type={key.key_type}, "
            f"input_shape={tuple(x_dev_u8.shape)}, "
            f"op_config={op_config.detach().cpu().tolist()}"
        )

    status = _torch_npu_crypto()(
        key_addr_tensor,
        x_dev_u8.contiguous(),
        out_dev_u8,
        iv_dev,
        op_config,
        tag_arg,
        None,
    )

    _check_status(status, name)
    _sync_npu(device)

    if key.alg_id == ALG_AES_GCM_128:
        if tag_arg is None:
            raise RuntimeError(f"{name}: GCM operator returned no tag tensor")

        if tag_arg.numel() != GCM_TAG_BYTES:
            raise RuntimeError(f"{name}: invalid GCM tag size: expected={GCM_TAG_BYTES}, actual={tag_arg.numel()}")

    return tag_arg


def _copy_cpu_cipher_to_dst(cipher_cpu_u8: torch.Tensor, dst_dev: torch.Tensor) -> torch.Tensor:
    cipher_cpu_typed = restore_from_byte_view(
        cipher_cpu_u8,
        dtype=dst_dev.dtype,
        shape=tuple(dst_dev.shape),
    )
    return dst_dev.copy_(cipher_cpu_typed, non_blocking=False)


def _validate_h2d_inputs(plain_cpu: torch.Tensor, dst_dev: torch.Tensor) -> None:
    if not isinstance(dst_dev, torch.Tensor):
        raise RuntimeError(f"dst_dev must be torch.Tensor, got={type(dst_dev)}")
    if dst_dev.device.type != "npu":
        raise RuntimeError(f"dst_dev must be NPU tensor, got device={dst_dev.device}")
    if plain_cpu.numel() != dst_dev.numel():
        raise RuntimeError(
            "secure_h2d_tensor_inplace requires same numel: "
            f"plain_shape={tuple(plain_cpu.shape)}, dst_shape={tuple(dst_dev.shape)}, "
            f"plain_numel={plain_cpu.numel()}, dst_numel={dst_dev.numel()}"
        )
    if plain_cpu.dtype != dst_dev.dtype:
        raise RuntimeError(
            "secure_h2d_tensor_inplace requires same dtype after prepare: "
            f"plain_dtype={plain_cpu.dtype}, dst_dtype={dst_dev.dtype}"
        )


def _encrypt_on_host(
    plain_u8: torch.Tensor, key: KeyContext, iv: bytes, alg_id: int
) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
    if alg_id == ALG_AES_CTR_128:
        return _host_ctr_crypt(plain_u8, key, iv, HOST_CTR_ENCRYPT), None
    if alg_id == ALG_AES_GCM_128:
        return _host_gcm_encrypt(plain_u8, key, iv)
    raise RuntimeError(f"unsupported alg_id={alg_id}")


def _decrypt_on_host(cipher_u8: torch.Tensor, ctx: SecureD2HContext) -> torch.Tensor:
    if ctx.alg_id == ALG_AES_CTR_128:
        return _host_ctr_crypt(cipher_u8, ctx.key, ctx.iv, HOST_CTR_DECRYPT)

    if ctx.alg_id == ALG_AES_GCM_128:
        if ctx.tag_cpu is None:
            raise RuntimeError("GCM D2H host decrypt requires tag_cpu in ctx")
        return _host_gcm_decrypt(cipher_u8, ctx.tag_cpu, ctx.key, ctx.iv)

    raise RuntimeError(f"unsupported alg_id={ctx.alg_id}")


def _get_next_iv(manager, *, device_id: int, key: KeyContext) -> bytes:
    return bytes(
        manager.next_iv(
            device_id=device_id,
            key=key,
            iv_bytes=CONFIG.iv_bytes,
        )
    )


def secure_h2d_tensor_inplace(
    plain_cpu: torch.Tensor,
    *,
    dst_dev: torch.Tensor,
    device_id: int | None = None,
    alg_id: int | None = None,
) -> torch.Tensor:
    """
    H2D:
        Host encrypt -> copy cipher to device -> Device decrypt.
    """
    alg_id = CONFIG.alg_id if alg_id is None else int(alg_id)
    _ensure_alg_supported(alg_id)
    _validate_h2d_inputs(plain_cpu, dst_dev)

    device = normalize_device(dst_dev.device)
    local_device_id = device_id_from_device(device) if device_id is None else int(device_id)
    global_device_id, kms_device_id, _ = _device_ids(local_device_id)

    manager = get_key_manager()
    plain_u8 = byte_view(plain_cpu.detach().contiguous()).reshape(-1)
    num_bytes = int(plain_u8.numel())
    errors = []

    if _debug_enabled():
        log(
            "H2D enter: "
            f"local_device={local_device_id}, global_device={global_device_id}, "
            f"kms_device={kms_device_id}, alg={alg_id}, "
            f"shape={tuple(dst_dev.shape)}, dtype={dst_dev.dtype}"
        )

    for key in manager.get_candidate_keys(
        device_id=local_device_id,
        alg_id=alg_id,
        key_type=KEY_TYPE_H2D,
    ):
        try:
            key_addr = _ensure_device_key_loaded(
                key=key,
                device=device,
                device_id=local_device_id,
            )
            iv = _get_next_iv(manager, device_id=local_device_id, key=key)

            cipher_cpu_u8, tag_cpu = _encrypt_on_host(plain_u8, key, iv, alg_id)
            _copy_cpu_cipher_to_dst(cipher_cpu_u8, dst_dev)

            dst_bytes = byte_view(dst_dev).reshape(-1)
            plain_tmp = torch.empty_like(dst_bytes)

            _device_crypt_to(
                dst_bytes,
                plain_tmp,
                key=key,
                key_addr_tensor=key_addr,
                iv=iv,
                device=device,
                local_device_id=local_device_id,
                mode=MODE_DEVICE_DECRYPT,
                name="DEVICE_DECRYPT_INPLACE",
                tag_cpu=tag_cpu,
            )

            dst_bytes.copy_(plain_tmp)
            _sync_npu(device)

            manager.record_success(device_id=local_device_id, key=key, num_bytes=num_bytes)
            return dst_dev

        except Exception as exc:
            errors.append(exc)
            manager.record_failure(device_id=local_device_id, key=key, error=exc)

    raise RuntimeError("H2D secure inplace copy failed: " + "; ".join(repr(e) for e in errors))


def attach_d2h_copy_event(
    ctx: SecureD2HContext,
    *,
    stream: object | None = None,
) -> SecureD2HContext:
    device = torch.device(f"npu:{ctx.local_device_id}")

    if stream is None:
        stream = torch.npu.current_stream(device)

    event = torch.npu.Event()
    event.record(stream)

    if _debug_enabled():
        log(f"D2H copy event recorded: local_device={ctx.local_device_id}, stream={stream}, event={event}")

    return replace(ctx, copy_event=event)


def secure_d2h_encrypt_for_async_copy(
    plain_dev: torch.Tensor,
    *,
    device_id: int | None = None,
    alg_id: int | None = None,
) -> Tuple[torch.Tensor, SecureD2HContext]:
    alg_id = CONFIG.alg_id if alg_id is None else int(alg_id)
    _ensure_alg_supported(alg_id)

    if not isinstance(plain_dev, torch.Tensor):
        raise RuntimeError(f"plain_dev must be torch.Tensor, got={type(plain_dev)}")
    if plain_dev.device.type != "npu":
        raise RuntimeError(f"plain_dev must be NPU tensor, got device={plain_dev.device}")

    device = normalize_device(plain_dev.device)
    local_device_id = device_id_from_tensor(plain_dev) if device_id is None else int(device_id)
    global_device_id, kms_device_id, _ = _device_ids(local_device_id)

    manager = get_key_manager()
    src = plain_dev.detach().contiguous()
    plain_u8 = byte_view(src).reshape(-1)
    num_bytes = int(plain_u8.numel())
    errors = []

    if _debug_enabled():
        log(
            "D2H enter: "
            f"local_device={local_device_id}, global_device={global_device_id}, "
            f"kms_device={kms_device_id}, alg={alg_id}, "
            f"shape={tuple(src.shape)}, dtype={src.dtype}"
        )

    for key in manager.get_candidate_keys(
        device_id=local_device_id,
        alg_id=alg_id,
        key_type=KEY_TYPE_D2H,
    ):
        try:
            key_addr = _ensure_device_key_loaded(
                key=key,
                device=device,
                device_id=local_device_id,
            )
            iv = _get_next_iv(manager, device_id=local_device_id, key=key)
            cipher_u8 = torch.empty_like(plain_u8)

            tag_dev = _device_crypt_to(
                plain_u8,
                cipher_u8,
                key=key,
                key_addr_tensor=key_addr,
                iv=iv,
                device=device,
                local_device_id=local_device_id,
                mode=MODE_DEVICE_ENCRYPT,
                name="DEVICE_ENCRYPT_FOR_ASYNC_D2H",
            )

            tag_cpu = None
            if alg_id == ALG_AES_GCM_128:
                if tag_dev is None:
                    raise RuntimeError("GCM D2H encrypt requires tag_dev")

                if tag_dev.numel() != GCM_TAG_BYTES:
                    raise RuntimeError(
                        f"GCM D2H encrypt returned invalid tag size: expected={GCM_TAG_BYTES}, actual={tag_dev.numel()}"
                    )

                tag_cpu = tag_dev.detach().reshape(-1).to(device="cpu", non_blocking=False).contiguous().clone()

                if tag_cpu.numel() != GCM_TAG_BYTES:
                    raise RuntimeError(
                        f"GCM D2H tag CPU copy has invalid size: expected={GCM_TAG_BYTES}, actual={tag_cpu.numel()}"
                    )

            encrypted_dev = restore_from_byte_view(
                cipher_u8,
                dtype=src.dtype,
                shape=tuple(src.shape),
            )
            encrypt_event = None
            encrypt_stream = None
            try:
                encrypt_stream = torch.npu.current_stream(device)
                encrypt_event = torch.npu.Event()
                encrypt_event.record(encrypt_stream)

                if _debug_enabled():
                    log(f"D2H encrypt event recorded: device={device}, stream={encrypt_stream}, event={encrypt_event}")
            except Exception as exc:
                if _debug_enabled():
                    log(f"D2H record encrypt event failed: {repr(exc)}")
                encrypt_event = None
                encrypt_stream = None

            ctx = SecureD2HContext(
                key=key,
                iv=bytes(iv),
                alg_id=alg_id,
                local_device_id=local_device_id,
                global_device_id=global_device_id,
                kms_device_id=kms_device_id,
                orig_dtype=src.dtype,
                orig_shape=tuple(src.shape),
                num_bytes=num_bytes,
                tag_cpu=tag_cpu,
                encrypt_event=encrypt_event,
                encrypt_stream=encrypt_stream,
            )

            return encrypted_dev, ctx

        except Exception as exc:
            errors.append(exc)
            manager.record_failure(device_id=local_device_id, key=key, error=exc)

    raise RuntimeError("D2H device encrypt failed: " + "; ".join(repr(e) for e in errors))


def secure_d2h_decrypt_after_async_copy(
    cipher_cpu: torch.Tensor,
    ctx: SecureD2HContext,
) -> torch.Tensor:
    manager = get_key_manager()

    try:
        if not isinstance(cipher_cpu, torch.Tensor):
            raise RuntimeError(f"cipher_cpu must be torch.Tensor, got={type(cipher_cpu)}")

        if cipher_cpu.device.type != "cpu":
            raise RuntimeError(
                f"secure_d2h_decrypt_after_async_copy expected a CPU tensor, got device={cipher_cpu.device}"
            )

        if ctx.alg_id == ALG_AES_GCM_128:
            if ctx.tag_cpu is None:
                raise RuntimeError("GCM D2H decrypt requires tag_cpu in context")

            if ctx.tag_cpu.device.type != "cpu":
                raise RuntimeError(f"GCM tag must be CPU tensor, got={ctx.tag_cpu.device}")

            if ctx.tag_cpu.numel() != GCM_TAG_BYTES:
                raise RuntimeError(
                    f"GCM D2H decrypt received invalid tag size: expected={GCM_TAG_BYTES}, actual={ctx.tag_cpu.numel()}"
                )

        cipher_contiguous = cipher_cpu.detach().contiguous()
        cipher_u8 = byte_view(cipher_contiguous).reshape(-1)

        if cipher_u8.numel() != ctx.num_bytes:
            raise RuntimeError(
                "D2H cipher size mismatch: "
                f"expected={ctx.num_bytes}, actual={cipher_u8.numel()}, "
                f"shape={tuple(cipher_cpu.shape)}, "
                f"dtype={cipher_cpu.dtype}"
            )

        if _debug_enabled() and ctx.alg_id == ALG_AES_GCM_128:
            log(
                "D2H GCM host decrypt: "
                f"local_device={ctx.local_device_id}, "
                f"global_device={ctx.global_device_id}, "
                f"kms_device={ctx.kms_device_id}, "
                f"key_id={ctx.key.key_id}, "
                f"iv={ctx.iv.hex()}, "
                f"tag={ctx.tag_cpu.reshape(-1).tolist()}, "
                f"num_bytes={cipher_u8.numel()}"
            )

        plain_u8 = _decrypt_on_host(cipher_u8, ctx)

        plain_cpu = restore_from_byte_view(
            plain_u8,
            dtype=ctx.orig_dtype,
            shape=ctx.orig_shape,
        )

        manager.record_success(
            device_id=ctx.local_device_id,
            key=ctx.key,
            num_bytes=ctx.num_bytes,
        )

        return plain_cpu

    except Exception as exc:
        manager.record_failure(
            device_id=ctx.local_device_id,
            key=ctx.key,
            error=exc,
        )
        raise
