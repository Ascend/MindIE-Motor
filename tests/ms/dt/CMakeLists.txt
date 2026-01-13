project(MindIE-MS-DT)

get_filename_component(MINDIE_SERVICE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../.." ABSOLUTE)

set(ENABLE_CONTROLLER ON)
set(ENABLE_COORDINATOR ON)
set(ENABLE_IPC ON)
set(ENABLE_SECURITYUTILS ON)
set(ENABLE_MINDIE_SERVICE_UTILS ON)

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR})
set(MINDIE_MS_SRC_ROOT ${MINDIE_SERVICE_ROOT}/mindie_service/management_service)
set(MINDIE_MS_TEST_ROOT "${MINDIE_SERVICE_ROOT}/tests/ms")

if(DEFINED ENV{BUILD_MIES_3RDPARTY_INSTALL_DIR})
    set(BUILD_MIES_3RDPARTY_INSTALL_DIR $ENV{BUILD_MIES_3RDPARTY_INSTALL_DIR})
    message(STATUS "Using BUILD_MIES_3RDPARTY_INSTALL_DIR from environment: ${BUILD_MIES_3RDPARTY_INSTALL_DIR}")
else()
    # 如果环境变量未定义，则设置为当前路径
    get_filename_component(DEFAULT_3RD_DIR "${CMAKE_CURRENT_SOURCE_DIR}/3rdparty" ABSOLUTE)
    set(BUILD_MIES_3RDPARTY_INSTALL_DIR "${DEFAULT_3RD_DIR}")
    message(STATUS "BUILD_MIES_3RDPARTY_INSTALL_DIR not found in environment, using current directory: ${BUILD_MIES_3RDPARTY_INSTALL_DIR}")
endif()

if(DEFINED ENV{BUILD_MINDIE_SERVICE_INSTALL_DIR})
    set(BUILD_MINDIE_SERVICE_INSTALL_DIR $ENV{BUILD_MINDIE_SERVICE_INSTALL_DIR})
    message(STATUS "Using BUILD_MINDIE_SERVICE_INSTALL_DIR from environment: ${BUILD_MINDIE_SERVICE_INSTALL_DIR}")
else()
    # 如果环境变量未定义，则设置为当前路径
    get_filename_component(DEFAULT_LIB_DIR "${MINDIE_MS_SRC_ROOT}/output" ABSOLUTE)
    set(BUILD_MINDIE_SERVICE_INSTALL_DIR "${DEFAULT_LIB_DIR}")
    message(STATUS "BUILD_MINDIE_SERVICE_INSTALL_DIR not found in environment, using current directory: ${BUILD_MINDIE_SERVICE_INSTALL_DIR}")
endif()

if(DEFINED ENV{GLOBAL_ABI_VERSION})
    add_compile_definitions(_GLIBCXX_USE_CXX11_ABI=$ENV{GLOBAL_ABI_VERSION})
    message(STATUS "Setting abi to: $ENV{GLOBAL_ABI_VERSION}")
endif()

set(GOOGLT_TEST_DIR ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/googletest)
set(CPP_STUB_DIR ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/cpp-stub)
set(HTTP_CLIENT_CTL_DIR ${MINDIE_SERVICE_ROOT}/mindie_service/utils/http_client_ctl)

include_directories(
    # 测试
    # 蓝区构建dt时会找到
    ${GOOGLT_TEST_DIR}/include/
    ${CPP_STUB_DIR}/src
    ${CPP_STUB_DIR}/src_linux

    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/boost/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/libboundscheck/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/nlohmann-json/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/openssl/include
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/grpc/include

    ${HTTP_CLIENT_CTL_DIR}/log
    ${HTTP_CLIENT_CTL_DIR}/http_client
    ${HTTP_CLIENT_CTL_DIR}/common
    ${HTTP_CLIENT_CTL_DIR}/params
    ${MINDIE_MS_TEST_ROOT}/dt/common/helper
    ${MINDIE_MS_TEST_ROOT}/dt/common/main
    ${MINDIE_MS_TEST_ROOT}/dt/common/thread_safe_vector
    ${MINDIE_MS_TEST_ROOT}/common/backtrace

    ${MINDIE_MS_SRC_ROOT}/build/open_source/boost
    ${MINDIE_MS_SRC_ROOT}/build/open_source/nlohmann/include
    ${MINDIE_MS_SRC_ROOT}/build/open_source/openssl/include
    $ENV{ASCEND_HOME_PATH}/include
)

if (EXISTS $ENV{ASCEND_HOME_PATH}/include/msServiceProfiler/msServiceProfiler.h)
    add_definitions(-DWITH_PROF)
endif()

link_directories(
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/googletest/lib
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/libboundscheck/lib
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/openssl/lib
    ${BUILD_MIES_3RDPARTY_INSTALL_DIR}/grpc/lib
    ${BUILD_MINDIE_SERVICE_INSTALL_DIR}/lib
)

set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -g -fPIC -fno-access-control -fno-inline -no-pie -fno-stack-protector -Wl,--allow-multiple-definition -Wno-pmf-conversions")

set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++17")
if("${BUILDING_STAGE}" STREQUAL "gcov")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fprofile-arcs -ftest-coverage")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fprofile-arcs -ftest-coverage")
elseif("${BUILDING_STAGE}" STREQUAL "hitest_cov")
    set_property(GLOBAL PROPERTY RULE_LAUNCH_COMPILE hitestwrapper)
    set_property(GLOBAL PROPERTY RULE_LAUNCH_LINK hitestwrapper)
elseif("${BUILDING_STAGE}" STREQUAL "asan")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fprofile-arcs -ftest-coverage")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fsanitize=address -fno-omit-frame-pointer -fsanitize-recover=address -fsanitize=leak")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fprofile-arcs -ftest-coverage")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fsanitize=address -fno-omit-frame-pointer -fsanitize-recover=address -fsanitize=leak")
elseif("${BUILDING_STAGE}" STREQUAL "fuzz")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fprofile-arcs -ftest-coverage -fsanitize-coverage=trace-pc")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fPIC -fno-omit-frame-pointer")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fprofile-arcs -ftest-coverage -fsanitize-coverage=trace-pc")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fPIC -fno-omit-frame-pointer")
elseif("${BUILDING_STAGE}" STREQUAL "tsan")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fPIE -fsanitize=thread")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fPIE -fsanitize=thread")
endif()

aux_source_directory(${MINDIE_MS_TEST_ROOT}/dt/common/concurrent_test TEST_FRAMEWORK_SOURCE_DIR)
aux_source_directory(${MINDIE_MS_TEST_ROOT}/dt/common/helper TEST_FRAMEWORK_SOURCE_DIR)
aux_source_directory(${MINDIE_MS_TEST_ROOT}/dt/common/main TEST_FRAMEWORK_SOURCE_DIR)
aux_source_directory(${MINDIE_MS_TEST_ROOT}/dt/common/thread_safe_vector TEST_FRAMEWORK_SOURCE_DIR)
aux_source_directory(${MINDIE_MS_TEST_ROOT}/common/backtrace TEST_FRAMEWORK_SOURCE_DIR)

add_library(test_framework SHARED ${TEST_FRAMEWORK_SOURCE_DIR})
target_link_libraries(test_framework)

if (ENABLE_CONTROLLER)
    add_subdirectory(controller)
endif()
if (ENABLE_COORDINATOR)
    add_subdirectory(coordinator)
endif()
if (ENABLE_IPC)
    add_subdirectory(ipc)
endif()
if (ENABLE_SECURITYUTILS)
    add_subdirectory(securityutils)
endif()
if (ENABLE_MINDIE_SERVICE_UTILS)
    add_subdirectory(mindie_service_utils)
endif()
