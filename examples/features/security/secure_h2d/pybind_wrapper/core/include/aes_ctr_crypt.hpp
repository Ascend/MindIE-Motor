// Copyright (c) 2026 Huawei Technologies Co., Ltd
#include <openssl/evp.h>
#include <cstdint>
#include <string>
#include <memory>
#include "torch/extension.h"

class AesCtrCryptException : public std::runtime_error {
  public:
    explicit AesCtrCryptException(const std::string &msg) : std::runtime_error(msg) {}
};

class AesCtrCrypt {
  public:
    // 修改构造函数参数类型
    AesCtrCrypt(const uint8_t *input, const int16_t *iv, const int16_t *key, bool mode);
    ~AesCtrCrypt();

    void AesCryptProcess(
        const uint8_t *input, uint8_t *output, size_t size, const int16_t *key, const int16_t *iv, bool encrypt);

  private:
    EVP_CIPHER_CTX *aesCtx = nullptr;
    bool isEncrypt;
};

at::Tensor aes_ctr_crypt(const at::Tensor &x, const at::Tensor &iv, const at::Tensor &key, const at::Tensor &mode);
