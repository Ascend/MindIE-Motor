#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

set -e

[ -z "$MINDIE_SERVICE_SRC_DIR" ] && echo "Error: MINDIE_SERVICE_SRC_DIR is not set, exit" && exit 1
[ -z "$BUILD_MINDIE_SERVICE_INSTALL_DIR" ] && echo "Error: BUILD_MINDIE_SERVICE_INSTALL_DIR is not set, exit" && exit 1
[ -z "$BUILD_MIES_3RDPARTY_INSTALL_DIR" ] && echo "Error: BUILD_MIES_3RDPARTY_INSTALL_DIR is not set, exit" && exit 1

SCRIPTS_DIR="$MINDIE_SERVICE_SRC_DIR"/ci
# Get the version of python, format it as 'py310' etc.
PY_VERSION=$(python3 --version | awk '{split($2, v, "."); print "py" v[1] v[2]}')
# Get the architecture of system, it should be "aarch64" or "x86_64"
ARCH=$(uname -m)

LOG_PATH="/var/log/mindie_log/"
LOG_NAME="mindie_service_install.log"
[ -z "$MINDIE_VERSION" ] && MINDIE_VERSION="1.0.0"
[ -z "$MINDIE_SERVICE_VERSION" ] && MINDIE_SERVICE_VERSION="$MINDIE_VERSION"

# discard in the future --- begin
BUILD_DIR=$(dirname $(readlink -f $0))
DEBUG="$1"
PROJ_ROOT_DIR=${BUILD_DIR}/..
MindIEServiceVersion="1.0.0"
if [ ! -f "${PROJ_ROOT_DIR}"/../CI/config/version.ini ]; then
    echo "version.ini is not exsited !"
else
    MindIEServiceVersion=$(cat ${PROJ_ROOT_DIR}/../CI/config/version.ini | grep "PackageName" | cut -d "=" -f 2)
fi
MindIEServiceVersion=$(echo $MindIEServiceVersion | sed -E 's/([0-9]+)\.([0-9]+)\.RC([0-9]+)\.([0-9]+)/\1.\2rc\3.post\4/')
echo "MindIEServiceVersion $MindIEServiceVersion"
MINDIE_VERSION="$MindIEServiceVersion"
MINDIE_SERVICE_VERSION="$MindIEServiceVersion"
echo "MINDIE_VERSION: $MINDIE_VERSION"
echo "MINDIE_SERVICE_VERSION: $MINDIE_SERVICE_VERSION"
# discard in the future --- end

export RELEASE_TMP_DIR=${MINDIE_SERVICE_SRC_DIR}/output/build/Ascend-mindie-service-${MINDIE_VERSION}-linux-${ARCH}
RELEASE_RUN_DIR=${MINDIE_SERVICE_SRC_DIR}/output/$ARCH
DEBUG_SYMBOLS_DIR=${MINDIE_SERVICE_SRC_DIR}/output/debug_symbols
mkdir -p "$RELEASE_TMP_DIR"
mkdir -p "$RELEASE_RUN_DIR"
mkdir -p "$DEBUG_SYMBOLS_DIR"

if [[ "${ARCH}" != "aarch64" && "${ARCH}" != "x86_64" ]]; then
    echo "Error: The system is not aarch64 or x86_64, exit."
    exit 1
fi


function bep_env_init() {
    # 使SECBEPKIT_HOME生效
    source /etc/profile
    # bep消除二进制
    local bep_env_config="$SCRIPTS_DIR"/bep_env.conf
    # 检查BepKit预置环境
    local bep_sh=$(which bep_env.sh)
    echo "has bep sh : ${bep_sh}"
    # 执行bep脚本
    if [ ! -d "${SECBEPKIT_HOME}" ] && [ ! -f "${bep_sh}" ]; then
        echo "BepKit is uninstalled, Please install the tool and configure the env var \$SECBEPKIT_HOME"
    else
        source ${SECBEPKIT_HOME}/bep_env.sh -s $bep_env_config
        if [ $? -ne 0 ]; then
            echo "build bep failed!"
            exit 1
        else
            echo "build bep success!"
        fi
    fi
}

function clean() {
    rm -rf "$RELEASE_TMP_DIR"
}

function set_version() {
cat>"$RELEASE_TMP_DIR"/version.info<<EOF
    Ascend-mindie : ${MINDIE_VERSION}
    Ascend-mindie-service Version : ${MINDIE_SERVICE_VERSION}
    Platform : ${ARCH}
EOF
}


function create_dir() {
    sub_dirs=("bin" "conf" "lib" "include" "logs" "scripts/http_client_ctl" "examples/kubernetes_deploy_scripts" \
              "security/certs" "security/ca" "security/keys" "security/pass")
    cd "$RELEASE_TMP_DIR"
    for dir in "${sub_dirs[@]}"; do
        mkdir -p  "$dir"
    done
}

function copy_mindie_service() {
    echo "Copy mindie service libraries ..."
    cd "$BUILD_MINDIE_SERVICE_INSTALL_DIR"/bin
    cp ms_coordinator ms_server http_client_ctl ms_controller msctl \
        "$RELEASE_TMP_DIR"/bin/

    cd "$BUILD_MINDIE_SERVICE_INSTALL_DIR"
    cp -rf config/* "$RELEASE_TMP_DIR"/conf/

    cd "$BUILD_MINDIE_SERVICE_INSTALL_DIR"/dist
    cp -f mindiebenchmark-*-py3-*.whl mindieclient-*-py3-*.whl mindiesimulator-*-py3-*.whl om_adapter-*-py3-*.whl node_manager-*-py3-*.whl \
          "$RELEASE_TMP_DIR"/bin/
    mkdir -p "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/conf
    cp -rf "$MINDIE_SERVICE_SRC_DIR"/mindie_service/management_service/example/deploy_scripts/*  "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/

    cp -f "$MINDIE_SERVICE_SRC_DIR"/mindie_service/management_service/config/ms_coordinator.json "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/conf/
    cp -f "$MINDIE_SERVICE_SRC_DIR"/mindie_service/management_service/config/ms_controller.json "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/conf/
    cp -f "$MINDIE_SERVICE_SRC_DIR"/mindie_service/management_service/config/node_manager.json "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/conf/
    cp -f "$MINDIE_SERVICE_SRC_DIR"/mindie_service/utils/http_client_ctl/config/http_client_ctl.json "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/conf/
    chmod 640 "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/conf/*.json
    chmod 640 "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/boot_helper/boot.sh
    chmod 640 "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/user_config.json
    chmod 640 "$RELEASE_TMP_DIR"/examples/kubernetes_deploy_scripts/deployment/*.yaml

    cd "$MINDIE_SERVICE_SRC_DIR"/mindie_service/utils/http_client_ctl
    mkdir -p "$RELEASE_TMP_DIR"/scripts/http_client_ctl
    cp -rf scripts/* "$RELEASE_TMP_DIR"/scripts/http_client_ctl/

    cd "$MINDIE_SERVICE_SRC_DIR"/build/service/
    mkdir -p "$RELEASE_TMP_DIR"/scripts/utils
    [ -f upgrade_info.json ] && cp -f upgrade_info.json "$RELEASE_TMP_DIR"/scripts/utils
    [ -f upgrade_server.py ] && cp -f upgrade_server.py "$RELEASE_TMP_DIR"/scripts/utils
}

function _copy_grpc() {
    grpc_dst_dir="$RELEASE_TMP_DIR"/lib/grpc
    mkdir -p "$grpc_dst_dir"
    grpc_so_version="so.2308.0.0"
    grpc_so_list=("libabsl_bad_optional_access.${grpc_so_version}" \
       "libabsl_bad_variant_access.${grpc_so_version}" \
       "libabsl_base.${grpc_so_version}" \
       "libabsl_city.${grpc_so_version}" \
       "libabsl_civil_time.${grpc_so_version}" \
       "libabsl_cord.${grpc_so_version}" \
       "libabsl_cord_internal.${grpc_so_version}" \
       "libabsl_cordz_functions.${grpc_so_version}" \
       "libabsl_cordz_handle.${grpc_so_version}" \
       "libabsl_cordz_info.${grpc_so_version}" \
       "libabsl_debugging_internal.${grpc_so_version}" \
       "libabsl_demangle_internal.${grpc_so_version}" \
       "libabsl_exponential_biased.${grpc_so_version}" \
       "libabsl_graphcycles_internal.${grpc_so_version}" \
       "libabsl_hash.${grpc_so_version}" \
       "libabsl_hashtablez_sampler.${grpc_so_version}" \
       "libabsl_int128.${grpc_so_version}" \
       "libabsl_log_severity.${grpc_so_version}" \
       "libabsl_low_level_hash.${grpc_so_version}" \
       "libabsl_malloc_internal.${grpc_so_version}" \
       "libabsl_raw_hash_set.${grpc_so_version}" \
       "libabsl_raw_logging_internal.${grpc_so_version}" \
       "libabsl_spinlock_wait.${grpc_so_version}" \
       "libabsl_stacktrace.${grpc_so_version}" \
       "libabsl_status.${grpc_so_version}" \
       "libabsl_statusor.${grpc_so_version}" \
       "libabsl_str_format_internal.${grpc_so_version}" \
       "libabsl_strerror.${grpc_so_version}" \
       "libabsl_strings.${grpc_so_version}" \
       "libabsl_string_view.${grpc_so_version}" \
       "libabsl_strings_internal.${grpc_so_version}" \
       "libabsl_symbolize.${grpc_so_version}" \
       "libabsl_synchronization.${grpc_so_version}" \
       "libabsl_throw_delegate.${grpc_so_version}" \
       "libabsl_time.${grpc_so_version}" \
       "libabsl_time_zone.${grpc_so_version}" \
       "libabsl_die_if_null.${grpc_so_version}" \
       "libabsl_log_internal_check_op.${grpc_so_version}" \
       "libabsl_log_internal_message.${grpc_so_version}" \
       "libabsl_log_internal_nullguard.${grpc_so_version}" \
       "libabsl_random_internal_pool_urbg.${grpc_so_version}" \
       "libabsl_random_internal_randen.${grpc_so_version}" \
       "libabsl_random_internal_randen_hwaes_impl.${grpc_so_version}" \
       "libabsl_random_internal_randen_slow.${grpc_so_version}" \
       "libabsl_flags_internal.${grpc_so_version}" \
       "libabsl_flags_marshalling.${grpc_so_version}" \
       "libabsl_flags_reflection.${grpc_so_version}" \
       "libabsl_crc_cord_state.${grpc_so_version}" \
       "libabsl_kernel_timeout_internal.${grpc_so_version}" \
       "libabsl_log_internal_conditions.${grpc_so_version}" \
       "libabsl_examine_stack.${grpc_so_version}" \
       "libabsl_log_internal_format.${grpc_so_version}" \
       "libabsl_log_internal_proto.${grpc_so_version}" \
       "libabsl_log_internal_log_sink_set.${grpc_so_version}" \
       "libabsl_log_internal_globals.${grpc_so_version}" \
       "libabsl_log_globals.${grpc_so_version}" \
       "libabsl_random_internal_seed_material.${grpc_so_version}" \
       "libabsl_random_seed_gen_exception.${grpc_so_version}" \
       "libabsl_random_internal_randen_hwaes.${grpc_so_version}" \
       "libabsl_random_internal_platform.${grpc_so_version}" \
       "libabsl_flags_commandlineflag.${grpc_so_version}" \
       "libabsl_flags_commandlineflag_internal.${grpc_so_version}" \
       "libabsl_flags_config.${grpc_so_version}" \
       "libabsl_flags_private_handle_accessor.${grpc_so_version}" \
       "libabsl_crc32c.${grpc_so_version}" \
       "libabsl_log_sink.${grpc_so_version}" \
       "libabsl_flags_program_name.${grpc_so_version}" \
       "libabsl_crc_internal.${grpc_so_version}" \
       "libabsl_crc_cpu_detect.${grpc_so_version}" \
       "libabsl_log_entry.${grpc_so_version}" \
       "libabsl_flags.${grpc_so_version}" \
       "libabsl_random_distributions.${grpc_so_version}" \
       "libabsl_random_seed_sequences.${grpc_so_version}" \
       "libabsl_leak_check.${grpc_so_version}" \
       "libabsl_log_initialize.${grpc_so_version}" \
       "libutf8_range_lib.so.37" \
       "libupb_collections_lib.so.37" \
       "libupb_textformat_lib.so.37" \
       "libupb_json_lib.so.37" \
       "libaddress_sorting.so.37" \
       "libgpr.so.37" \
       "libgrpc++.so.1.60" \
       "libgrpc++_reflection.so.1.60" \
       "libgrpc.so.37" \
       "libprotobuf.so.25.1.0" \
       "libre2.so.11" \
       "libupb.so.37" )
    for item in "${grpc_so_list[@]}"; do
        cp -f "$BUILD_MIES_3RDPARTY_INSTALL_DIR"/grpc/lib/${item} "$grpc_dst_dir"
    done
}

function copy_third_party() {
    _copy_grpc

    cd "$BUILD_MIES_3RDPARTY_INSTALL_DIR"/prometheus-cpp/lib
    cp -f libprometheus-cpp-core.so.* libprometheus-cpp-pull.so.* "$RELEASE_TMP_DIR"/lib
    cp -d libprometheus-cpp-core.so libprometheus-cpp-core.so.* \
          libprometheus-cpp-pull.so libprometheus-cpp-pull.so.* \
          "$RELEASE_TMP_DIR"/lib

    cd "$BUILD_MIES_3RDPARTY_INSTALL_DIR"/boost/lib
    cp -f libboost_chrono.so.1.87.0 libboost_thread.so.1.87.0 "$RELEASE_TMP_DIR"/lib

    cd "$BUILD_MIES_3RDPARTY_INSTALL_DIR"/libboundscheck/lib
    cp -f libboundscheck.so "$RELEASE_TMP_DIR"/lib

    cd "$BUILD_MIES_3RDPARTY_INSTALL_DIR"/openssl/lib
    cp -f libcrypto.so* libssl.so* "$RELEASE_TMP_DIR"/lib
}

function copy_install_uninstall_script() {
    mkdir -p "$RELEASE_TMP_DIR"/scripts
    cp -f "$SCRIPTS_DIR"/install/uninstall.sh "$RELEASE_TMP_DIR"/scripts
    cp -f "$SCRIPTS_DIR"/install/install.sh "$RELEASE_TMP_DIR"

    sed -i "s!VERSION=replace_version!VERSION=${MINDIE_VERSION}!" "$RELEASE_TMP_DIR"/install.sh
    sed -i "s!LOG_PATH=replace_log_path!LOG_PATH=${LOG_PATH}!" "$RELEASE_TMP_DIR"/install.sh
    sed -i "s!LOG_NAME=replace_log_name!LOG_NAME=${LOG_NAME}!" "$RELEASE_TMP_DIR"/install.sh
    sed -i "s!ARCH=replace_arch!ARCH=${ARCH}!" "$RELEASE_TMP_DIR"/install.sh

    sed -i "s!VERSION=replace_version!VERSION=${MINDIE_VERSION}!" "$RELEASE_TMP_DIR"/scripts/uninstall.sh
    sed -i "s!LOG_PATH=replace_log_path!LOG_PATH=${LOG_PATH}!" "$RELEASE_TMP_DIR"/scripts/uninstall.sh
    sed -i "s!LOG_NAME=replace_log_name!LOG_NAME=${LOG_NAME}!" "$RELEASE_TMP_DIR"/scripts/uninstall.sh
}

function prepare_files() {
    copy_third_party
    copy_mindie_service
    copy_install_uninstall_script
}

function package() {
    makeself_dir=$BUILD_MIES_3RDPARTY_INSTALL_DIR/makeself
    [ ! -f "$makeself_dir"/makeself.sh ] && echo "Error: makeself.sh is not found, exit" && exit 1

    # makeself mindie-rt.run
    chmod +x "$RELEASE_TMP_DIR"/install.sh
    $makeself_dir/makeself.sh --header "$SCRIPTS_DIR"/makeself-header.sh --help-header "$SCRIPTS_DIR"/install/help.info \
        --gzip --complevel 4 --nomd5 --sha256 --chown ${RELEASE_TMP_DIR} \
        $RELEASE_RUN_DIR/Ascend-mindie-service_${MINDIE_VERSION}_${PY_VERSION}_linux-${ARCH}.run \
        "Ascend-mindie-service" ./install.sh
}

function package_debug_symbols() {
    debug_symbols_package_name="$DEBUG_SYMBOLS_DIR/Ascend-mindie-motor-debug-symbols_${MINDIE_VERSION}_${PY_VERSION}_linux-${ARCH}.tar.gz"
    cd "$BUILD_MINDIE_SERVICE_INSTALL_DIR"
    tar czpf $debug_symbols_package_name motor_debug_symbols
}

function main() {
    bep_env_init
    set_version
    create_dir
    prepare_files
    package
    if [[ "$DEBUG" == "false" ]]; then
        package_debug_symbols
    fi
}

main
