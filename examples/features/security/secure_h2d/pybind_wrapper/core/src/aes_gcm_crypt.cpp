// Copyright (c) 2026 Huawei Technologies Co., Ltd
#include "aes_gcm_crypt.hpp"

#include <openssl/err.h>
#include <cstring>
#include <stdexcept>
#include <climits>
#include <cstdio>

namespace {
constexpr size_t GCM_RECOMM_IV_LEN = 16; // PyTorch 侧仍按 int16[8] / 16 bytes 传入
constexpr size_t GCM_RECOMM_TAG_LEN = 16;
constexpr size_t AES128_KEY_LEN = 16;

inline void print_openssl_err(const char *where) {
    unsigned long e = ERR_get_error();
    if (e) {
        char buf[256] = {0};
        ERR_error_string_n(e, buf, sizeof(buf));
        std::fprintf(stderr, "[OpenSSL][%s] %s\n", where, buf);
    }
}
} // namespace

AesGcmCrypt::AesGcmCrypt(const uint8_t * /*input*/, const int16_t * /*iv*/, const int16_t * /*key*/, bool mode)
    : ctx_(nullptr), isEncrypt_(mode) {
    ctx_ = EVP_CIPHER_CTX_new();
    if (!ctx_) {
        throw AesGcmCryptException("EVP_CIPHER_CTX_new failed");
    }
}

AesGcmCrypt::~AesGcmCrypt() {
    if (ctx_) {
        EVP_CIPHER_CTX_free(ctx_);
        ctx_ = nullptr;
    }
}

void AesGcmCrypt::AesGcmProcess(const uint8_t *input, uint8_t *output, size_t in_size, const int16_t *key,
    size_t key_size, const int16_t *iv, size_t iv_size, const uint8_t *aad, size_t aad_size, const uint8_t *tag_in,
    size_t tag_in_size, bool encrypt, uint8_t *tag_out, size_t tag_out_size) {
    (void)aad;
    (void)aad_size;

    if (!input || !output || !key || !iv) {
        throw AesGcmCryptException("Null pointer passed to AesGcmProcess");
    }

    if (key_size != AES128_KEY_LEN) {
        throw AesGcmCryptException("Only AES-128-GCM with 16 bytes key is supported");
    }

    // 与 AICPU 侧当前入参保持一致：iv tensor 为 int16[8]，底层共 16 bytes。
    // 注意：为了严格匹配 AICPU aes_128_gcm_encrypt/decrypt 的 OpenSSL 调用，
    // 这里不调用 EVP_CTRL_GCM_SET_IVLEN。OpenSSL GCM 默认 IV 长度为 12 bytes，
    // 因此实际参与 GCM 初始化的是 iv 指针起始处的默认 IV 长度语义。
    if (iv_size != GCM_RECOMM_IV_LEN) {
        throw AesGcmCryptException("IV tensor must be 16 bytes to match current AICPU input contract");
    }

    if (in_size == 0) {
        throw AesGcmCryptException("AES_GCM_128 input size is 0");
    }
    if (in_size > static_cast<size_t>(INT_MAX)) {
        throw AesGcmCryptException("AES_GCM_128 input size too large: AICPU path only accepts <= INT_MAX bytes");
    }

    if (encrypt) {
        if (tag_out == nullptr || tag_out_size != GCM_RECOMM_TAG_LEN) {
            throw AesGcmCryptException("tag_out must be 16 bytes for encryption output");
        }
    } else {
        if (tag_in == nullptr || tag_in_size != GCM_RECOMM_TAG_LEN) {
            throw AesGcmCryptException("tag_in must be 16 bytes for decryption");
        }
    }

    const unsigned char *key_bytes = reinterpret_cast<const unsigned char *>(key);
    const unsigned char *iv_bytes = reinterpret_cast<const unsigned char *>(iv);

    int rc = 0;
    int len = 0;
    uint32_t out_len = 0;

    if (encrypt) {
        // 对齐 AICPU aes_128_gcm_encrypt：一次 Init 直接传 key/iv；不设置 IVLEN；不处理 AAD；不分块。
        rc = EVP_EncryptInit_ex(ctx_, EVP_aes_128_gcm(), nullptr, key_bytes, iv_bytes);
        if (rc != 1) {
            print_openssl_err("EVP_EncryptInit_ex AES_GCM_128");
            throw AesGcmCryptException("EVP_EncryptInit_ex AES_GCM_128 failed");
        }

        rc = EVP_EncryptUpdate(ctx_, output, &len, input, static_cast<int>(in_size));
        if (rc != 1) {
            print_openssl_err("EVP_EncryptUpdate AES_GCM_128");
            throw AesGcmCryptException("EVP_EncryptUpdate AES_GCM_128 failed");
        }
        out_len += static_cast<uint32_t>(len);

        rc = EVP_EncryptFinal_ex(ctx_, nullptr, &len);
        if (rc != 1) {
            print_openssl_err("EVP_EncryptFinal_ex AES_GCM_128");
            throw AesGcmCryptException("EVP_EncryptFinal_ex AES_GCM_128 failed");
        }

        rc = EVP_CIPHER_CTX_ctrl(ctx_, EVP_CTRL_GCM_GET_TAG, static_cast<int>(GCM_RECOMM_TAG_LEN), tag_out);
        if (rc != 1) {
            print_openssl_err("EVP_CTRL_GCM_GET_TAG AES_GCM_128");
            throw AesGcmCryptException("EVP_CTRL_GCM_GET_TAG AES_GCM_128 failed");
        }

        if (out_len != in_size) {
            throw AesGcmCryptException("AES_GCM_128 encrypt output length mismatch");
        }
        return;
    }

    // 对齐 AICPU aes_128_gcm_decrypt：一次 Init 直接传 key/iv；不设置 IVLEN；
    // 先 SET_TAG，再 DecryptUpdate；不处理 AAD；不分块；Final 仅做认证校验。
    rc = EVP_DecryptInit_ex(ctx_, EVP_aes_128_gcm(), nullptr, key_bytes, iv_bytes);
    if (rc != 1) {
        print_openssl_err("EVP_DecryptInit_ex AES_GCM_128");
        throw AesGcmCryptException("EVP_DecryptInit_ex AES_GCM_128 failed");
    }

    rc = EVP_CIPHER_CTX_ctrl(
        ctx_, EVP_CTRL_GCM_SET_TAG, static_cast<int>(GCM_RECOMM_TAG_LEN), const_cast<uint8_t *>(tag_in));
    if (rc != 1) {
        print_openssl_err("EVP_CTRL_GCM_SET_TAG AES_GCM_128");
        throw AesGcmCryptException("EVP_CTRL_GCM_SET_TAG AES_GCM_128 failed");
    }

    rc = EVP_DecryptUpdate(ctx_, output, &len, input, static_cast<int>(in_size));
    if (rc != 1) {
        print_openssl_err("EVP_DecryptUpdate AES_GCM_128");
        throw AesGcmCryptException("EVP_DecryptUpdate AES_GCM_128 failed");
    }
    out_len += static_cast<uint32_t>(len);

    rc = EVP_DecryptFinal_ex(ctx_, nullptr, &len);
    if (rc != 1) {
        print_openssl_err("EVP_DecryptFinal_ex AES_GCM_128");
        throw AesGcmCryptException("EVP_DecryptFinal_ex AES_GCM_128 failed (authentication failure)");
    }

    if (out_len != in_size) {
        throw AesGcmCryptException("AES_GCM_128 decrypt output length mismatch");
    }
}

// PyTorch wrapper: 返回 (out, tag_out)
std::tuple<at::Tensor, at::Tensor> aes_gcm_crypt(const at::Tensor &x, const at::Tensor &iv, const at::Tensor &key,
    const at::Tensor &mode, const at::Tensor &aad, const at::Tensor &tag_in) {
    try {
        TORCH_CHECK(x.is_contiguous(), "Input tensor must be contiguous");
        TORCH_CHECK(iv.is_contiguous() && key.is_contiguous(), "IV/Key must be contiguous");
        TORCH_CHECK(x.device().is_cpu(), "Input must be CPU tensor");
        TORCH_CHECK(iv.device().is_cpu() && key.device().is_cpu(), "IV/Key must be CPU tensor");

        TORCH_CHECK(x.scalar_type() == torch::kUInt8, "Input must be uint8");
        TORCH_CHECK(iv.scalar_type() == torch::kInt16, "IV must be int16");
        TORCH_CHECK(key.scalar_type() == torch::kInt16, "Key must be int16");
        TORCH_CHECK(mode.scalar_type() == torch::kInt16 || mode.scalar_type() == torch::kInt32,
            "Mode must be int16 or int32 (0=decrypt,1=encrypt)");

        bool encrypt = false;
        if (mode.scalar_type() == torch::kInt16) {
            encrypt = (mode.item<int16_t>() != 0);
        } else {
            encrypt = (mode.item<int32_t>() != 0);
        }

        // AICPU 当前 IsNeedAad() 固定 false，aes_128_gcm_encrypt/decrypt 也没有 AAD Update。
        // 这里保留接口参数，但不参与计算，以保证 Host 结果与 AICPU GCM128 路径一致。
        const uint8_t *aad_ptr = nullptr;
        size_t aad_size = 0;
        if (aad.defined() && aad.numel() > 0) {
            TORCH_CHECK(aad.scalar_type() == torch::kUInt8, "AAD must be uint8");
            aad_ptr = aad.data_ptr<uint8_t>();
            aad_size = static_cast<size_t>(aad.numel() * aad.element_size());
        }

        const uint8_t *tag_in_ptr = nullptr;
        size_t tag_in_size = 0;
        if (tag_in.defined() && tag_in.numel() > 0) {
            TORCH_CHECK(tag_in.scalar_type() == torch::kUInt8, "tag_in must be uint8");
            TORCH_CHECK(tag_in.device().is_cpu(), "tag_in must be CPU tensor");
            tag_in_ptr = tag_in.data_ptr<uint8_t>();
            tag_in_size = static_cast<size_t>(tag_in.numel() * tag_in.element_size());
        }

        at::Tensor out = torch::empty_like(x);
        at::Tensor tag_out = torch::empty({static_cast<long>(GCM_RECOMM_TAG_LEN)}, torch::dtype(torch::kUInt8));

        AesGcmCrypt cryptor(x.data_ptr<uint8_t>(), iv.data_ptr<int16_t>(), key.data_ptr<int16_t>(), encrypt);

        cryptor.AesGcmProcess(x.data_ptr<uint8_t>(), out.data_ptr<uint8_t>(),
            static_cast<size_t>(x.numel() * x.element_size()), key.data_ptr<int16_t>(),
            static_cast<size_t>(key.numel() * key.element_size()), iv.data_ptr<int16_t>(),
            static_cast<size_t>(iv.numel() * iv.element_size()), aad_ptr, aad_size, tag_in_ptr, tag_in_size, encrypt,
            tag_out.data_ptr<uint8_t>(), static_cast<size_t>(tag_out.numel() * tag_out.element_size()));

        return std::make_tuple(out, tag_out);
    } catch (const AesGcmCryptException &e) {
        PyErr_SetString(PyExc_RuntimeError, e.what());
        throw py::error_already_set();
    } catch (const std::exception &e) {
        PyErr_SetString(PyExc_RuntimeError, e.what());
        print_openssl_err("aes_gcm_crypt wrapper");
        throw py::error_already_set();
    }
}
