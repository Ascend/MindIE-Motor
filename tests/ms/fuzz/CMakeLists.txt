project(MindIE-MS-DT)

get_filename_component(MINDIE_SERVICE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../.." ABSOLUTE)

set(ENABLE_CONTROLLER ON)
set(ENABLE_COORDINATOR ON)

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR})
set(MINDIE_MS_SRC_ROOT ${MINDIE_SERVICE_ROOT}/mindie_service/management_service)
set(MINDIE_MS_TEST_ROOT "${MINDIE_SERVICE_ROOT}/tests/ms")

set(GOOGLT_TEST_DIR ${MINDIE_MS_TEST_ROOT}/open_source/googletest)
set(EMOCK_DIR ${MINDIE_MS_TEST_ROOT}/open_source/emock)
set(CPPFREEMOCK_DIR ${MINDIE_MS_TEST_ROOT}/open_source/emock)
set(CPP_STUB_DIR ${MINDIE_MS_TEST_ROOT}/open_source/cpp-stub)

set(SECODEFUZZ_TEST_DIR ${MINDIE_MS_TEST_ROOT}/open_source/secodefuzz)
set(HTTP_CLIENT_CTL_DIR ${MINDIE_SERVICE_ROOT}/mindie_service/utils/http_client_ctl)

if(DEFINED ENV{GLOBAL_ABI_VERSION})
    add_compile_definitions(_GLIBCXX_USE_CXX11_ABI=$ENV{GLOBAL_ABI_VERSION})
    message(STATUS "Setting abi to: $ENV{GLOBAL_ABI_VERSION}")
endif()

file(GLOB TEST_MAIN_SRC
    ${MINDIE_MS_TEST_ROOT}/dt/common/helper/*.cpp
    ${MINDIE_MS_TEST_ROOT}/dt/common/mindie_server/*.cpp
    ${MINDIE_MS_TEST_ROOT}/dt/common/request_helper/*.cpp
    ${MINDIE_MS_TEST_ROOT}/fuzz/common/*.cpp
)

include_directories(
    # 测试
    ${GOOGLT_TEST_DIR}/googletest/include
    ${GOOGLT_TEST_DIR}/googlemock/include
    ${CPP_STUB_DIR}/src
    #changes
    ${MINDIE_MS_SRC_ROOT}/build/open_source/kmc/include
    ${MINDIE_MS_SRC_ROOT}/build/open_source/libboundscheck/include
    ${MINDIE_MS_TEST_ROOT}/fuzz/common
    ${SECODEFUZZ_TEST_DIR}/Secodefuzz
    ${SECODEFUZZ_TEST_DIR}/Secodepits
    ${HTTP_CLIENT_CTL_DIR}/log
    ${HTTP_CLIENT_CTL_DIR}/http_client
    ${HTTP_CLIENT_CTL_DIR}/common
    ${HTTP_CLIENT_CTL_DIR}/params
)

add_subdirectory(${GOOGLT_TEST_DIR} gtest)

link_directories(
    ${MINDIE_MS_SRC_ROOT}/build/open_source/openssl
    #changes
    ${MINDIE_MS_SRC_ROOT}/build/open_source/kmc/lib
    ${MINDIE_MS_SRC_ROOT}/build/open_source/libboundscheck/lib
    ${MINDIE_MS_SRC_ROOT}/output/lib
    ${SECODEFUZZ_TEST_DIR}/OpenSource/test/test1
    ${SECODEFUZZ_TEST_DIR}/OpenSource/test/test1
)


set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++17 -fno-access-control -fPIC")
if("${BUILDING_STAGE}" STREQUAL "gcov")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fprofile-arcs -ftest-coverage")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fprofile-arcs -ftest-coverage")
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

if (ENABLE_CONTROLLER)
    add_subdirectory(controller)
endif()
if (ENABLE_COORDINATOR)
    add_subdirectory(coordinator)
endif()