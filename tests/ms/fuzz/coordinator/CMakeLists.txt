set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)

message(${MINDIE_MS_SRC_ROOT}/coordinator/request_listener/)
include_directories(
    # 源码
    ${MINDIE_MS_SRC_ROOT}/
    ${MINDIE_MS_SRC_ROOT}/common
    # ${MINDIE_MS_SRC_ROOT}/common/http_server
    ${MINDIE_MS_SRC_ROOT}/common/http_client
    ${MINDIE_MS_SRC_ROOT}/coordinator
    ${MINDIE_MS_SRC_ROOT}/coordinator/configure
    ${MINDIE_MS_SRC_ROOT}/coordinator/cluster_monitor
    ${MINDIE_MS_SRC_ROOT}/coordinator/request_monitor
    ${MINDIE_MS_SRC_ROOT}/coordinator/request_listener
    ${MINDIE_MS_SRC_ROOT}/coordinator/request_repeater
    ${MINDIE_MS_SRC_ROOT}/coordinator/controller_listener
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/http_client
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/http_server
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/communication
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/io_context_pool
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/data_structure
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/connection_pool
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/timer/
    ${MINDIE_MS_SRC_ROOT}/coordinator/request_repeater
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler
    ${MINDIE_MS_SRC_ROOT}/coordinator/metrics
    ${MINDIE_MS_SRC_ROOT}/coordinator/processor
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm/round_robin
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm/cache_affinity
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm/load_balance
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/node_store
    ${MINDIE_MS_SRC_ROOT}/coordinator/request
    ${MINDIE_MS_SRC_ROOT}/coordinator/resource
    ${MINDIE_MS_SRC_ROOT}/coordinator/digs_scheduler

    #三方
    ${MINDIE_MS_SRC_ROOT}/build/open_source/boost
    ${MINDIE_MS_SRC_ROOT}/build/open_source/nlohmann/include
    ${MINDIE_MS_SRC_ROOT}/build/open_source/nlohmann/include/nlohmann
    ${MINDIE_MS_SRC_ROOT}/build/open_source/openssl/include
    ${MINDIE_MS_SRC_ROOT}/build/open_source
    ${MINDIE_MS_TEST_ROOT}/dt/common/helper
    ${MINDIE_MS_TEST_ROOT}/dt/common/mindie_server
    ${MINDIE_MS_TEST_ROOT}/dt/common/request_helper
    ${MINDIE_MS_TEST_ROOT}/dt/common/thread_safe_vector
)

set(COORDINATOR_SOURCE_DIR ${MINDIE_MS_SRC_ROOT}/coordinator)
set(HTTP_CLIENT_CTL_DIR ${MINDIE_SERVICE_ROOT}/mindie_service/utils/http_client_ctl)
file(GLOB_RECURSE COORDINATOR_SOURCE_DIR "${COORDINATOR_SOURCE_DIR}/*.cpp" "${COORDINATOR_SOURCE_DIR}/*.c" "${COORDINATOR_SOURCE_DIR}/*.cxx")

file(GLOB COMMON_SOURCE_DIR
    ${MINDIE_MS_SRC_ROOT}/common/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/http_client/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/log/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/http_client/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/common/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/params/*.cpp
)

list(REMOVE_ITEM COORDINATOR_SOURCE_DIR "${MINDIE_MS_SRC_ROOT}/coordinator/main.cpp")

add_library(test_coordinator SHARED ${COORDINATOR_SOURCE_DIR} ${COMMON_SOURCE_DIR})
target_link_libraries(test_coordinator crypto ssl boundscheck pthread -ldl)

aux_source_directory(${CMAKE_CURRENT_SOURCE_DIR} COORDINATOR_SRCS_FILES)

add_executable(mindie_ms_coordinator_fuzz ${COORDINATOR_SRCS_FILES} ${TEST_MAIN_SRC})

link_directories(${MINDIE_MS_SRC_ROOT}/output/lib)
target_link_libraries(mindie_ms_coordinator_fuzz gtest)
target_link_libraries(mindie_ms_coordinator_fuzz crypto ssl)
target_link_libraries(mindie_ms_coordinator_fuzz Secodefuzz)
target_link_libraries(mindie_ms_coordinator_fuzz Secodepits)
target_link_libraries(mindie_ms_coordinator_fuzz xml2)
target_link_libraries(mindie_ms_coordinator_fuzz boundscheck)
target_link_libraries(mindie_ms_coordinator_fuzz test_coordinator)

target_link_libraries(mindie_ms_coordinator_fuzz dl pthread)
