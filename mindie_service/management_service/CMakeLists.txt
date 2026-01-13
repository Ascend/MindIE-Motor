project(MindIE-MS)

cmake_minimum_required(VERSION 3.18)
set(CMAKE_CXX_STANDARD 17)

option(ENABLE_CONTROLLER "Enable controller module" ON)
option(ENABLE_COORDINATOR "Enable coordinator module" ON)

if(NOT DEFINED ENV{BUILD_MIES_3RDPARTY_INSTALL_DIR})
    message(FATAL_ERROR "BUILD_MIES_3RDPARTY_INSTALL_DIR is not set.")
endif()
set(BUILD_MIES_3RDPARTY_INSTALL_DIR $ENV{BUILD_MIES_3RDPARTY_INSTALL_DIR})
message("Set BUILD_MIES_3RDPARTY_INSTALL_DIR as ${BUILD_MIES_3RDPARTY_INSTALL_DIR}.")

if(DEFINED ENV{GLOBAL_ABI_VERSION})
    add_compile_definitions(_GLIBCXX_USE_CXX11_ABI=$ENV{GLOBAL_ABI_VERSION})
    message(STATUS "Setting abi to: $ENV{GLOBAL_ABI_VERSION}")
endif()

IF(${CMAKE_BUILD_TYPE} STREQUAL "Debug")
    ADD_COMPILE_OPTIONS(-fPIE -fstack-protector-all -fPIC -std=c++17 -g -O0 --coverage -Werror=narrowing
        -D_FORTIFY_SOURCE=2 -ftrapv -fno-common)
    ADD_LINK_OPTIONS(--coverage)
ELSE()
    ADD_COMPILE_OPTIONS(-fPIE -fstack-protector-all -fPIC -std=c++17 -g -O3 -D_FORTIFY_SOURCE=2 -ftrapv
        -Wall -Wextra -Wfloat-equal -Werror=narrowing  -fno-common)
    ADD_LINK_OPTIONS(-pie -Wl,-z,relro -Wl,-z,now -Wl,-z,noexecstack,--no-as-needed)
ENDIF()

SET(CMAKE_SKIP_RPATH TRUE)

if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif ()

if("${BUILDING_STAGE}" STREQUAL "gcov")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fprofile-arcs -ftest-coverage")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fprofile-arcs -ftest-coverage")
elseif("${BUILDING_STAGE}" STREQUAL "asan")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fprofile-arcs -ftest-coverage")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fsanitize=address -fno-omit-frame-pointer -fsanitize-recover=address -fsanitize=leak")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fprofile-arcs -ftest-coverage")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fsanitize=address -fno-omit-frame-pointer -fsanitize-recover=address -fsanitize=leak")
elseif("${BUILDING_STAGE}" STREQUAL "tsan")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fPIE -fsanitize=thread")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fPIE -fsanitize=thread")
endif()

if (EXISTS $ENV{ASCEND_HOME_PATH}/include/msServiceProfiler/msServiceProfiler.h)
    add_definitions(-DWITH_PROF)
endif()

include_directories(
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/boost/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/libboundscheck/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/nlohmann-json/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/openssl/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/grpc/include
    $ENV{ASCEND_HOME_PATH}/include
)

link_directories(
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/libboundscheck/lib
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/openssl/lib
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/grpc/lib
)

set(ROOT_PATH ${CMAKE_CURRENT_SOURCE_DIR})
set(UTILS_HTTP_CLIENT_CTL_PATH ${CMAKE_CURRENT_SOURCE_DIR}/../utils/http_client_ctl)
if (ENABLE_CONTROLLER)
    add_subdirectory(controller)
endif()
if (ENABLE_COORDINATOR)
    add_subdirectory(coordinator)
endif()
