// Copyright (c) 2026 Huawei Technologies Co., Ltd
#ifndef _AES_GCM_CRYPT_HPP_
#define _AES_GCM_CRYPT_HPP_

#include <openssl/evp.h>
#include <cstdint>
#include <string>
#include <memory>
#include "torch/extension.h"

class AesGcmCryptException : public std::runtime_error {
  public:
    explicit AesGcmCryptException(const std::string &msg) : std::runtime_error(msg) {}
};

class AesGcmCrypt {
  public:
    // 构造函数：与 aes_ctr 风格一致（传入指针仅为风格统一）
    AesGcmCrypt(const uint8_t *input, const int16_t *iv, const int16_t *key, bool mode);
    ~AesGcmCrypt();
    void AesGcmProcess(const uint8_t *input, uint8_t *output, size_t in_size, const int16_t *key, size_t key_size,
        const int16_t *iv, size_t iv_size, const uint8_t *aad, size_t aad_size, const uint8_t *tag_in,
        size_t tag_in_size, bool encrypt, uint8_t *tag_out, size_t tag_out_size);

  private:
    EVP_CIPHER_CTX *ctx_;
    bool isEncrypt_;
};

/// PyTorch wrapper: 返回 std::tuple<at::Tensor, at::Tensor> (out, tag_out)
std::tuple<at::Tensor, at::Tensor> aes_gcm_crypt(const at::Tensor &x, const at::Tensor &iv, const at::Tensor &key,
    const at::Tensor &mode, const at::Tensor &aad = at::Tensor(), const at::Tensor &tag_in = at::Tensor());

#endif // _AES_GCM_CRYPT_HPP_
