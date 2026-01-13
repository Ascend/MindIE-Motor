project(MindIE-MS-DT)
include_directories(
    # 源码
    ${MINDIE_MS_SRC_ROOT}/
    ${MINDIE_MS_SRC_ROOT}/common
    ${MINDIE_MS_SRC_ROOT}/common/http_client
    ${MINDIE_MS_SRC_ROOT}/common/ipc/
    ${MINDIE_MS_SRC_ROOT}/common/digs/
    ${MINDIE_MS_SRC_ROOT}/common/alarm_config
    ${MINDIE_MS_SRC_ROOT}/common/ipc/heartbeat
    ${MINDIE_MS_SRC_ROOT}/common/ipc/shared_memory
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/grpc_proto/
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/etcd_proto/

    ${MINDIE_MS_SRC_ROOT}/coordinator/common/connection_pool
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/data_structure
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/http_client
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/http_server
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/io_context_pool
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/communication
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/timer
    ${MINDIE_MS_SRC_ROOT}/coordinator/common/utils
    ${MINDIE_MS_SRC_ROOT}/coordinator/alarm
    ${MINDIE_MS_SRC_ROOT}/coordinator/processor
    ${MINDIE_MS_SRC_ROOT}/coordinator/configure
    ${MINDIE_MS_SRC_ROOT}/coordinator/metrics
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler
    ${MINDIE_MS_SRC_ROOT}/coordinator/digs_scheduler
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm/round_robin
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm/cache_affinity
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/algorithm/load_balance
    ${MINDIE_MS_SRC_ROOT}/coordinator/scheduler/default_scheduler/node_store
    ${MINDIE_MS_SRC_ROOT}/coordinator/cluster_monitor
    ${MINDIE_MS_SRC_ROOT}/coordinator/leader_judge
    ${MINDIE_MS_SRC_ROOT}/coordinator/request_monitor
    ${MINDIE_MS_SRC_ROOT}/coordinator/request_repeater
    ${MINDIE_MS_SRC_ROOT}/coordinator/request_listener
    ${MINDIE_MS_SRC_ROOT}/coordinator/controller_listener
    ${MINDIE_MS_SRC_ROOT}/coordinator
    ${MINDIE_MS_TEST_ROOT}/dt/common/mindie_server
    ${MINDIE_MS_TEST_ROOT}/dt/common/request_helper
    ${MINDIE_MS_TEST_ROOT}/dt/common/concurrent_test
    ${MINDIE_MS_TEST_ROOT}/dt/coordinator/request
    ${MINDIE_MS_TEST_ROOT}/dt/coordinator/resource
    ${MINDIE_MS_TEST_ROOT}/dt/coordinator/digs_scheduler
    ${MINDIE_MS_TEST_ROOT}/dt/coordinator/cluster_monitor_test
    ${MINDIE_MS_TEST_ROOT}/dt/coordinator
)

file(GLOB TEST_COORDINATOR_FRAMEWORK
    ${MINDIE_MS_TEST_ROOT}/dt/common/mindie_server/*.cpp
    ${MINDIE_MS_TEST_ROOT}/dt/common/request_helper/*.cpp
)

set(COORDINATOR_SOURCE_DIR ${MINDIE_MS_SRC_ROOT}/coordinator)
set(HTTP_CLIENT_CTL_DIR ${MINDIE_SERVICE_ROOT}/mindie_service/utils/http_client_ctl)
file(GLOB_RECURSE COORDINATOR_SOURCE_DIR "${COORDINATOR_SOURCE_DIR}/*.cpp" "${COORDINATOR_SOURCE_DIR}/*.c" "${COORDINATOR_SOURCE_DIR}/*.cxx")

file(GLOB COMMON_SOURCE_DIR
    ${MINDIE_MS_SRC_ROOT}/common/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/http_client/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/digs/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/alarm_config/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/grpc_proto/*.cc
    ${MINDIE_MS_SRC_ROOT}/common/ipc/heartbeat/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/ipc/shared_memory/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/etcd_proto/*.cc
    ${HTTP_CLIENT_CTL_DIR}/log/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/http_client/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/common/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/params/*.cpp
)

list(REMOVE_ITEM COORDINATOR_SOURCE_DIR "${MINDIE_MS_SRC_ROOT}/coordinator/main.cpp")

find_package(Protobuf REQUIRED CONFIG)
find_package(gRPC CONFIG REQUIRED)
set(_PROTOBUF_LIBPROTOBUF protobuf::libprotobuf)
set(_REFLECTION gRPC::grpc++_reflection)
set(_GRPC_GRPCPP gRPC::grpc++)
add_library(test_coordinator SHARED ${COORDINATOR_SOURCE_DIR} ${COMMON_SOURCE_DIR})
target_link_libraries(test_coordinator crypto ssl boundscheck pthread -ldl ${_REFLECTION} ${_GRPC_GRPCPP} ${_PROTOBUF_LIBPROTOBUF})

set(COORDINATOR_TEST_DIR ${MINDIE_MS_TEST_ROOT}/dt/coordinator)
file(GLOB_RECURSE COORDINATOR_TEST_SOURCE "${COORDINATOR_TEST_DIR}/*.cpp" "${COORDINATOR_TEST_DIR}/*.c" "${COORDINATOR_TEST_DIR}/*.cxx")

add_executable(mindie_ms_coordinator_ut ${COORDINATOR_TEST_SOURCE} ${TEST_COORDINATOR_FRAMEWORK} ${COMMON_SOURCE_DIR})
target_link_libraries(mindie_ms_coordinator_ut gtest gmock test_coordinator test_framework)
target_link_libraries(mindie_ms_coordinator_ut dl pthread boundscheck ssl crypto rt)
