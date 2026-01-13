set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)
set(HTTP_CLIENT_CTL_DIR ${MINDIE_SERVICE_ROOT}/mindie_service/utils/http_client_ctl)

set(MINDIE_MS_SRCS
    ${MINDIE_MS_SRC_ROOT}/common
    ${MINDIE_MS_SRC_ROOT}/common/http_server
    ${MINDIE_MS_SRC_ROOT}/common/http_client
    ${MINDIE_MS_TEST_ROOT}/fuzz/common
    ${MINDIE_MS_TEST_ROOT}/dt/common/helper

    ${CMAKE_CURRENT_SOURCE_DIR}/
    ${CMAKE_CURRENT_SOURCE_DIR}/../../common/helper/
    ${CMAKE_CURRENT_SOURCE_DIR}/../../common/thread_pool/

    ${ROOT_PATH}/common/
    ${ROOT_PATH}/common/http_client/

    ${MINDIE_MS_SRC_ROOT}/controller
    ${MINDIE_MS_SRC_ROOT}/controller/cluster_status
    ${MINDIE_MS_SRC_ROOT}/controller/constant
    ${MINDIE_MS_SRC_ROOT}/controller/json_file_loader
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/group_generator
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/rank_table_loader
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/role_manager_initializer
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/role_switcher
    ${MINDIE_MS_SRC_ROOT}/controller/role_manager
    ${MINDIE_MS_SRC_ROOT}/controller/probe_server
    ${MINDIE_MS_SRC_ROOT}/controller/process_manager
    ${MINDIE_MS_SRC_ROOT}/controller/request_handler
    ${MINDIE_MS_SRC_ROOT}/controller/status_updater
    ${HTTP_CLIENT_CTL_DIR}/log
    ${HTTP_CLIENT_CTL_DIR}/http_client
    ${HTTP_CLIENT_CTL_DIR}/common
    ${HTTP_CLIENT_CTL_DIR}/params

)

include_directories(
    # 源码
    ${MINDIE_MS_SRC_ROOT}/
    ${MINDIE_MS_SRC_ROOT}/common
    ${MINDIE_MS_SRC_ROOT}/common/http_server
    ${MINDIE_MS_SRC_ROOT}/common/http_client

    ${MINDIE_MS_SRC_ROOT}/controller/
    ${MINDIE_MS_SRC_ROOT}/controller/cluster_status
    ${MINDIE_MS_SRC_ROOT}/controller/constant
    ${MINDIE_MS_SRC_ROOT}/controller/json_file_loader
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/group_generator
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/rank_table_loader
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/role_manager_initializer
    ${MINDIE_MS_SRC_ROOT}/controller/node_scheduler/role_switcher
    ${MINDIE_MS_SRC_ROOT}/controller/probe_server
    ${MINDIE_MS_SRC_ROOT}/controller/process_manager
    ${MINDIE_MS_SRC_ROOT}/controller/request_handler
    ${MINDIE_MS_SRC_ROOT}/controller/status_updater
    ${MINDIE_MS_SRC_ROOT}/controller/role_manager

    #三方
    ${MINDIE_MS_SRC_ROOT}/build/open_source/boost
    ${MINDIE_MS_SRC_ROOT}/build/open_source/nlohmann/include
    ${MINDIE_MS_SRC_ROOT}/build/open_source/nlohmann/include/nlohmann
    ${MINDIE_MS_SRC_ROOT}/build/open_source/openssl/include
    ${MINDIE_MS_SRC_ROOT}/build/open_source
    ${MINDIE_MS_TEST_ROOT}/dt/common/helper
)

foreach(DIR ${MINDIE_MS_SRCS})
    aux_source_directory(${DIR} CONTROLLER_SRCS_FILES)
endforeach()


list(REMOVE_ITEM CONTROLLER_SRCS_FILES "${MINDIE_MS_SRC_ROOT}/controller/main.cpp")

aux_source_directory(${CMAKE_CURRENT_SOURCE_DIR} CONTROLLER_SRCS_FILES)

add_executable(mindie_ms_controller_fuzz ${CONTROLLER_SRCS_FILES})
target_link_libraries(mindie_ms_controller_fuzz gtest)
target_link_libraries(mindie_ms_controller_fuzz crypto ssl)
target_link_libraries(mindie_ms_controller_fuzz Secodefuzz)
target_link_libraries(mindie_ms_controller_fuzz Secodepits)
target_link_libraries(mindie_ms_controller_fuzz xml2)
target_link_libraries(mindie_ms_controller_fuzz boundscheck)

target_link_libraries(mindie_ms_controller_fuzz dl pthread)
