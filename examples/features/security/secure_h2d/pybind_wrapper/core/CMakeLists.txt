set(LIBRARY_OUTPUT_PATH ${CMAKE_CURRENT_BINARY_DIR}/../lib_output)
set(CMAKE_C_FLAGS   "${CMAKE_C_FLAGS}   -O2 -DNDEBUG -Wall")
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${TORCH_CXX_FLAGS} -O2 -DNDEBUG -Wall")

# 修复：使用CMake识别到的Python3.11，不再硬编码python3
execute_process(
  COMMAND bash -c "SITE_CUSTOMIZE_SKIP=1 VLLM_PLUGINS='' ${Python3_EXECUTABLE} -c 'import torch; print(torch.__path__[0])'"
  OUTPUT_STRIP_TRAILING_WHITESPACE
  OUTPUT_VARIABLE TORCH_PATH
)
message("✅ TORCH_PATH = ${TORCH_PATH}")

set(Torch_DIR "${TORCH_PATH}/share/cmake/Torch")
find_package(Torch REQUIRED)

find_library(CRYPTO_LIB
  NAMES libcrypto.so crypto
  PATHS /usr/local/ssl/lib /usr/lib /usr/lib/aarch64-linux-gnu /usr/lib/x86_64-linux-gnu
)
message(STATUS "✅ 找到 libcrypto: ${CRYPTO_LIB}")

include_directories(
    ${PY11_T_ROOT_DIR}/core/utils
    ${pybind11_INCLUDE_DIRS}
        include
    /usr/local/ssl/include
    ${TORCH_PATH}/include
    ${TORCH_PATH}/include/torch/csrc/api/include
)

# ========== 调整顺序：先创建模块目标，再链接 ==========
# aes_ctr_crypt 模块：先pybind11_add_module，再target_link_libraries
pybind11_add_module(aes_ctr_crypt
        python_wrapper/aes_ctr_wrapper.cpp
        src/aes_ctr_crypt.cpp
)
find_library(TORCH_PYTHON_LIB NAMES libtorch_python.so PATHS ${TORCH_PATH}/lib)
target_link_libraries(aes_ctr_crypt
  PUBLIC
  ${CRYPTO_LIB}
  ${TORCH_LIBRARIES}
  ${TORCH_PYTHON_LIB}
)

# aes_gcm_crypt 模块：同上，先创建再链接
pybind11_add_module(aes_gcm_crypt
        python_wrapper/aes_gcm_wrapper.cpp
        src/aes_gcm_crypt.cpp
)
find_library(TORCH_PYTHON_LIB NAMES libtorch_python.so PATHS ${TORCH_PATH}/lib)
target_link_libraries(aes_gcm_crypt
  PUBLIC
  ${CRYPTO_LIB}
  ${TORCH_LIBRARIES}
  ${TORCH_PYTHON_LIB}
)
