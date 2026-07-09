// Copyright (c) 2026 Huawei Technologies Co., Ltd
// All rights reserved.
//
// Licensed under the BSD 3-Clause License  (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// https://opensource.org/licenses/BSD-3-Clause
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "op_plugin/OpApiInterface.h"
#include "op_plugin/utils/op_api_common.h"

namespace op_api {
using npu_preparation = at_npu::native::OpPreparation;

at::Tensor crypto_nocheck(const at::Tensor &key, const at::Tensor &input, at::Tensor &output, const at::Tensor &iv,
    const at::Tensor &opConfig, at::Tensor &tagRefOptional, at::Tensor &aadRefOptional) {
    c10::SmallVector<int64_t> out_size = {1};
    at::ScalarType out_type = at::ScalarType::UInt32;
    auto options = c10::TensorOptions().device(c10::DeviceType::PrivateUse1).dtype(out_type);
    at::Tensor z = npu_preparation::apply_tensor_without_format(out_size, options);

    EXEC_NPU_CMD(aclnnCrypto, key, input, output, iv, opConfig, tagRefOptional, aadRefOptional, z);
    return z;
}
at::Tensor crypto(const at::Tensor &key, const at::Tensor &input, at::Tensor &output, const at::Tensor &iv,
    const at::Tensor &opConfig, at::Tensor &tagRefOptional, at::Tensor &aadRefOptional) {
    return crypto_nocheck(key, input, output, iv, opConfig, tagRefOptional, aadRefOptional);
}
} // namespace acl_op
