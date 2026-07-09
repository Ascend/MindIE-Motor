// Copyright (c) 2026 Huawei Technologies Co., Ltd
#include "aes_ctr_crypt.hpp"
#include <openssl/err.h>
#include <stdexcept>

namespace {
constexpr size_t AES_BLOCK_SIZE = 16;
constexpr size_t KEY_IV_ELEM_COUNT = 8;
} // namespace

// 构造函数实现
AesCtrCrypt::AesCtrCrypt(const uint8_t *input, const int16_t *iv, const int16_t *key, bool mode) : isEncrypt(mode) {
    // 初始化OpenSSL上下文
    aesCtx = EVP_CIPHER_CTX_new();
    if (!aesCtx) {
        throw AesCtrCryptException("EVP_CIPHER_CTX_new failed");
    }
}

// 析构函数实现
AesCtrCrypt::~AesCtrCrypt() {
    if (aesCtx) {
        EVP_CIPHER_CTX_free(aesCtx);
    }
}

// 核心加密/解密方法实现
void AesCtrCrypt::AesCryptProcess(
    const uint8_t *input, uint8_t *output, size_t size, const int16_t *key, const int16_t *iv, bool encrypt) {
    // 准备密钥和IV（转换为uint8_t数组）
    uint8_t key_bytes[AES_BLOCK_SIZE];
    uint8_t iv_bytes[AES_BLOCK_SIZE];

    // 确保内存拷贝安全
    std::memcpy(key_bytes, key, AES_BLOCK_SIZE);
    std::memcpy(iv_bytes, iv, AES_BLOCK_SIZE);

    // 设置AES-CTR参数
    if (EVP_CipherInit_ex(aesCtx, EVP_aes_128_ctr(), nullptr, key_bytes, iv_bytes, encrypt ? 1 : 0) != 1) {
        throw AesCtrCryptException("EVP_CipherInit_ex failed");
    }

    // 禁用填充
    if (EVP_CIPHER_CTX_set_padding(aesCtx, 0) != 1) {
        throw AesCtrCryptException("Failed to disable padding");
    }

    // 处理数据
    int out_len = 0;
    if (EVP_CipherUpdate(aesCtx, output, &out_len, input, size) != 1) {
        throw AesCtrCryptException("EVP_CipherUpdate failed");
    }
}

// PyTorch接口函数实现
at::Tensor aes_ctr_crypt(const at::Tensor &x, const at::Tensor &iv, const at::Tensor &key, const at::Tensor &mode) {
    try {
        // 增强输入验证
        TORCH_CHECK(x.is_contiguous(), "Input tensor must be contiguous");
        TORCH_CHECK(iv.is_contiguous() && key.is_contiguous(), "Key/IV must be contiguous");
        TORCH_CHECK(x.device().is_cpu(), "Input must be CPU tensor");

        // 检查数据类型
        TORCH_CHECK(x.scalar_type() == torch::kUInt8, "Input must be uint8");
        TORCH_CHECK(key.scalar_type() == torch::kInt16, "Key must be int16");
        TORCH_CHECK(iv.scalar_type() == torch::kInt16, "IV must be int16");
        TORCH_CHECK(mode.scalar_type() == torch::kInt16, "Mode must be int16");

        // 准备输出张量
        at::Tensor output = torch::empty_like(x);

        // 执行加密/解密
        AesCtrCrypt cryptor(
            x.data_ptr<uint8_t>(), iv.data_ptr<int16_t>(), key.data_ptr<int16_t>(), mode.item<int16_t>() != 0);

        cryptor.AesCryptProcess(x.data_ptr<uint8_t>(), output.data_ptr<uint8_t>(), x.numel() * x.element_size(),
            key.data_ptr<int16_t>(), iv.data_ptr<int16_t>(), mode.item<int16_t>() != 0);

        return output;
    } catch (const std::exception &e) {
        PyErr_SetString(PyExc_RuntimeError, e.what());
        throw py::error_already_set();
    }
}
