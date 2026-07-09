#!/usr/bin/env bash
set -euo pipefail
cur_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${cur_dir}"
echo "当前编译根目录: ${cur_dir}"
PYTHON_BIN="$(python -c 'import sys; print(sys.executable)')"
PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(sys.version.split()[0])')"
echo "当前 Python: ${PYTHON_BIN}"
echo "Python 版本: ${PYTHON_VERSION}"
"${PYTHON_BIN}" -m pip install pybind11 --quiet
PYBIND11_DIR="$("${PYTHON_BIN}" -c 'import pybind11; print(pybind11.get_cmake_dir())')"
echo "pybind11 CMake 路径: ${PYBIND11_DIR}"
rm -rf "${cur_dir}/build"
mkdir -p "${cur_dir}/lib_output"
cmake -S "${cur_dir}" \
    -B "${cur_dir}/build" \
    -G "Unix Makefiles" \
    -DPYTHON_EXECUTABLE="${PYTHON_BIN}" \
    -Dpybind11_DIR="${PYBIND11_DIR}"
make -C "${cur_dir}/build" -j"$(nproc)"

echo
echo "================ 编译完成 ================"
echo "使用 Python: ${PYTHON_BIN}"
echo "产出 so 目录: ${cur_dir}/lib_output/"
ls -lh "${cur_dir}/lib_output/"
