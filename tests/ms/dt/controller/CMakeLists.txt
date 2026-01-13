set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)
set(HTTP_CLIENT_CTL_DIR ${MINDIE_SERVICE_ROOT}/mindie_service/utils/http_client_ctl)

file(GLOB ms_controller_llt_source_files
    ${CMAKE_CURRENT_SOURCE_DIR}/*.cpp
    ${MINDIE_MS_TEST_ROOT}/dt/controller/digs_common/*.cpp
    ${MINDIE_MS_TEST_ROOT}/dt/controller/digs_include/*.cpp
    ${MINDIE_MS_TEST_ROOT}/dt/controller/stub/*.cpp
    ${MINDIE_MS_TEST_ROOT}/dt/controller/rolemanager/*.cpp
)

include_directories(
    ${MINDIE_MS_TEST_ROOT}/dt/controller/digs_common
    ${MINDIE_MS_TEST_ROOT}/dt/controller/digs_include
    ${MINDIE_MS_TEST_ROOT}/dt/controller/stub
    ${MINDIE_MS_TEST_ROOT}/dt/controller/rolemanager
    # 源码
    ${MINDIE_MS_SRC_ROOT}/
    ${MINDIE_MS_SRC_ROOT}/common
    ${MINDIE_MS_SRC_ROOT}/common/utils/
    ${MINDIE_MS_SRC_ROOT}/common/ipc/
    ${MINDIE_MS_SRC_ROOT}/common/ipc/heartbeat
    ${MINDIE_MS_SRC_ROOT}/common/ipc/shared_memory
    ${MINDIE_MS_SRC_ROOT}/common/http_server
    ${MINDIE_MS_SRC_ROOT}/common/http_client
    ${MINDIE_MS_SRC_ROOT}/common/alarm_config/
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/grpc_proto/
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/etcd_proto/

    ${MINDIE_MS_SRC_ROOT}/controller/
    ${MINDIE_MS_SRC_ROOT}/controller/ccae_reporter/
    ${MINDIE_MS_SRC_ROOT}/controller/json_file_loader/
    ${MINDIE_MS_SRC_ROOT}/controller/alarm_manager/
    ${MINDIE_MS_SRC_ROOT}/controller/alarm_manager/alarm_listener/
    ${MINDIE_MS_SRC_ROOT}/controller/constant/
    ${MINDIE_MS_SRC_ROOT}/controller/cluster_status/
    ${MINDIE_MS_SRC_ROOT}/controller/process_manager/
    ${MINDIE_MS_SRC_ROOT}/controller/request_handler/
    ${MINDIE_MS_SRC_ROOT}/controller/probe_server/
    ${MINDIE_MS_SRC_ROOT}/controller/status_updater/
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/group_generator/
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/rank_table_loader/
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/role_manager_initializer/
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/role_switcher/
    ${MINDIE_MS_SRC_ROOT}/controller/resource_manager/
    ${MINDIE_MS_SRC_ROOT}/controller/fault_manager/
    ${MINDIE_MS_SRC_ROOT}/controller/cluster_grpc/
    ${MINDIE_MS_SRC_ROOT}/common/ipc/
    ${MINDIE_MS_SRC_ROOT}/common/digs/
    ${MINDIE_MS_SRC_ROOT}/common/securityutils/
    ${MINDIE_MS_SRC_ROOT}/common/ipc/shared_memory/
    ${MINDIE_MS_SRC_ROOT}/controller/leader_judge/
    ${MINDIE_MS_SRC_ROOT}/controller/cluster_status/
    ${MINDIE_MS_SRC_ROOT}/controller/role_manager/
    ${MINDIE_MS_SRC_ROOT}/controller/common/
)

set(CONTROLLER_SOURCE_DIR ${MINDIE_MS_SRC_ROOT}/controller)

file(GLOB_RECURSE CONTROLLER_SOURCE_DIR "${CONTROLLER_SOURCE_DIR}/*.cpp" "${CONTROLLER_SOURCE_DIR}/*.c" "${CONTROLLER_SOURCE_DIR}/*.cxx" "${CONTROLLER_SOURCE_DIR}/*.cc")

list(REMOVE_ITEM CONTROLLER_SOURCE_DIR "${MINDIE_MS_SRC_ROOT}/controller/main.cpp")

file(GLOB COMMON_SOURCE_DIR
    ${MINDIE_MS_SRC_ROOT}/common/ipc/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/digs/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/ipc/shared_memory/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/alarm_config/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/cluster_grpc/grpc_proto/*.cc
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/leader_judge/etcd_proto/*.cc
    ${MINDIE_MS_SRC_ROOT}/common/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/http_client/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/http_server/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/ipc/heartbeat/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/ipc/shared_memory/*.cpp
    ${MINDIE_MS_SRC_ROOT}/common/securityutils/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/log/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/http_client/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/common/*.cpp
    ${HTTP_CLIENT_CTL_DIR}/params/*.cpp
)

find_package(Protobuf REQUIRED CONFIG)
find_package(gRPC CONFIG REQUIRED)
set(_PROTOBUF_LIBPROTOBUF protobuf::libprotobuf)
set(_REFLECTION gRPC::grpc++_reflection)
set(_GRPC_GRPCPP gRPC::grpc++)
add_library(test_controller SHARED ${CONTROLLER_SOURCE_DIR} ${COMMON_SOURCE_DIR})
target_link_libraries(test_controller crypto ssl boundscheck pthread -ldl ${_REFLECTION} ${_GRPC_GRPCPP} ${_PROTOBUF_LIBPROTOBUF})

set(SRCS_FILES ${ms_controller_llt_source_files})
add_executable(mindie_ms_controller_ut ${SRCS_FILES})
target_link_libraries(mindie_ms_controller_ut gtest gmock)
target_link_libraries(mindie_ms_controller_ut crypto ssl boundscheck)
target_link_libraries(mindie_ms_controller_ut dl pthread)
target_link_libraries(mindie_ms_controller_ut test_controller test_framework)

set(CMAKE_BUILD_TYPE Debug)
