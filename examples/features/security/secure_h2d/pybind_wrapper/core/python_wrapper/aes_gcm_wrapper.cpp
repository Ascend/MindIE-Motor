// Copyright (c) 2026 Huawei Technologies Co., Ltd
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "aes_gcm_crypt.hpp"

namespace py = pybind11;

PYBIND11_MODULE(aes_gcm_crypt, m) {
    m.doc() = "AES-128-GCM (NIST SP 800-38D) wrapper"; // optional module docstring

    m.def("aes_gcm_cryption", &aes_gcm_crypt, py::arg("x"), py::arg("iv"), py::arg("key"), py::arg("mode"),
        py::arg("aad") = at::Tensor(), py::arg("tag_in") = at::Tensor(),
        "aes gcm crypt; returns tuple (out, tag_out). mode: 1=encrypt, 0=decrypt");
}
