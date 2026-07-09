// Copyright (c) 2026 Huawei Technologies Co., Ltd
#include <pybind11/pybind11.h>

#include "aes_ctr_crypt.hpp"

namespace py = pybind11;

PYBIND11_MODULE(aes_ctr_crypt, m) {
    m.doc() = " NIST AES CTR 128"; // optional module docstring

    m.def("aes_ctr_cryption", &aes_ctr_crypt, py::arg("x"), py::arg("iv"), py::arg("key"), py::arg("mode"),
        py::return_value_policy::take_ownership, "aes ctr crypt");
}
