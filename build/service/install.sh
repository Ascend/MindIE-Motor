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

install_flag=n
install_path_flag=n
install_dir=""
sourcedir=$PWD
target_dir=""
default_install_path="/usr/local/Ascend/mindie-service"
VERSION=replace_version
LOG_PATH=replace_log_path
LOG_NAME=replace_log_name
py_install_path=$(python3 -c "import site; print(site.getsitepackages()[0])")
ARCH=replace_arch

if [ "$UID" = "0" ]; then
    log_file=${LOG_PATH}${LOG_NAME}
else
    cur_owner=$(whoami)
    LOG_PATH="/home/${cur_owner}${LOG_PATH}"
    log_file=${LOG_PATH}${LOG_NAME}
fi

function log() {
    if [ "x$log_file" = "x" ] || [ ! -f "$log_file" ]; then
        echo -e "[mindie-service] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2"
    else
        echo -e "[mindie-service] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2" >>$log_file
    fi
}

function print() {
    if [ "x$log_file" = "x" ] || [ ! -f "$log_file" ]; then
        echo -e "[mindie-service] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2"
    else
        echo -e "[mindie-service] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2" | tee -a $log_file
    fi
}

function make_dir() {
    log "INFO" "mkdir ${1}"
    mkdir -p ${1} 2>/dev/null
    if [ $? -ne 0 ]; then
        print "ERROR" "create $1 failed !"
        exit 1
    fi
}

function make_file() {
    log "INFO" "touch ${1}"
    touch ${1} 2>/dev/null
    if [ $? -ne 0 ]; then
        print "ERROR" "create $1 failed !"
        exit 1
    fi
}

function log_init() {
    if [ ! -d "$LOG_PATH" ]; then
        make_dir "$LOG_PATH"
    fi
    if [ ! -f "$log_file" ]; then
        make_file "$log_file"
        chmod_recursion ${LOG_PATH} "750" "dir"
        chmod 640 ${log_file}
    else
        local filesize=$(ls -l $log_file | awk '{ print $5}')
        local maxsize=$((1024*1024*50))
        if [ $filesize -gt $maxsize ]; then
            local log_file_move_name="ascend_mindie_service_install_bak.log"
            mv -f ${log_file} ${LOG_PATH}${log_file_move_name}
            chmod 440 ${LOG_PATH}${log_file_move_name}
            make_file "$log_file"
            chmod 640 ${log_file}
            log "INFO" "log file > 50M, move ${log_file} to ${LOG_PATH}${log_file_move_name}."
        fi
    fi
    print "INFO" "Install log save in ${log_file}"
}

function chmod_authority() {
    # Install related permission
    chmod_dir ${default_install_path} "750"
    chmod_file ${default_install_path}
    chmod_file ${install_dir}

    # Log related permission
    chmod_dir ${LOG_PATH} "750"
    chmod_file ${log_file}

    # Python related permission
    chmod_python

    # Script related permission
    chmod 550 ${install_dir}/scripts/uninstall.sh
    chmod -R 500 ${install_dir}/scripts
    chmod 550 ${install_dir}/scripts
    chmod 550 ${install_dir}/scripts/utils

    chmod -R 550 ${install_dir}/bin
    chmod_file ${install_dir}/bin
    chmod 500 ${install_dir}/bin/seceasy_encrypt
    chmod 500 ${install_dir}/bin/gen_cert
    chmod 550 ${install_dir}/lib
    chmod 550 ${install_dir}/lib/grpc
    chmod 440 ${install_dir}/lib/libsec_easy_utils.a
    chmod_recursion ${install_dir}/lib "440" "file" "*.so*"

    chmod 500 ${install_dir}/include
    chmod -R 700 ${install_dir}/security

    chmod_file ${install_dir}/examples/kubernetes_deploy_scripts
    chmod 550 ${install_dir}/examples/kubernetes_deploy_scripts
    chmod 550 ${install_dir}/examples/kubernetes_deploy_scripts/boot_helper
    chmod 550 ${install_dir}/examples/kubernetes_deploy_scripts/gen_ranktable_helper
    chmod 750 ${install_dir}/examples/kubernetes_deploy_scripts/conf
    chmod 750 ${install_dir}/examples/kubernetes_deploy_scripts/deployment
}

function chmod_file() {
    chmod_recursion ${1} "440" "file" "*.cpp"
    chmod_recursion ${1} "550" "file" "*.py"
    chmod_recursion ${1} "440" "file" "*.pyc"
    chmod_recursion ${1} "440" "file" "*.h"
    chmod_recursion ${1} "440" "file" "*.so"
    chmod_recursion ${1} "640" "file" "*.json"
    chmod_recursion ${1} "640" "file" "*.yaml"
    chmod_recursion ${1} "440" "file" "*.info"
    chmod_recursion ${1} "440" "file" "*.md"
    chmod_recursion ${1} "440" "file" "*.egg-info"
    chmod_recursion ${1} "550" "file" "*.sh"
    chmod_recursion ${1} "640" "file" "*.log"
    chmod_recursion ${1} "640" "file" "*.conf"
    chmod_recursion ${1} "440" "file" "*.whl"
}

function chmod_dir() {
    chmod_recursion ${1} ${2} "dir"
}

function chmod_python() {
    chmod_dir ${py_install_path}/mindiebenchmark "750"
    chmod_file ${py_install_path}/mindiebenchmark

    chmod_dir ${py_install_path}/mindieclient "750"
    chmod_file ${py_install_path}/mindieclient

    chmod_dir ${py_install_path}/om_adapter "750"
    chmod_file ${py_install_path}/om_adapter

    chmod_dir ${py_install_path}/node_manager "750"
    chmod_file ${py_install_path}/node_manager
}

function chmod_recursion() {
    local rights=$2
    if [ "$3" = "dir" ]; then
        find $1 -type d -exec chmod ${rights} {} \; 2>/dev/null
    elif [ "$3" = "file" ]; then
        find $1 -type f -name $4 -exec chmod ${rights} {} \; 2>/dev/null
    fi
}

function chmod_recursion() {
    local rights=$2
    if [ "$3" = "dir" ]; then
        find $1 -type d -exec chmod ${rights} {} \; 2>/dev/null
    elif [ "$3" = "file" ]; then
        find $1 -type f -name "$4" -exec chmod ${rights} {} \; 2>/dev/null
    fi
}

function parse_script_args() {
    while true
    do
        case "$1" in
        --quiet)
            QUIET="y"
            shift
        ;;
        --install)
        install_flag=y
        shift
        ;;
        --install-path=*)
        install_path_flag=y
        target_dir=$(echo $1 | cut -d"=" -f2-)
        target_dir=${target_dir}/mindie-service
        shift
        ;;
        --*)
        shift
        ;;
        *)
        break
        ;;
        esac
    done
}

function check_path() {
    local cur_uid=$(id -u)
    local install_path_uid=0

    if [ ! -d "${install_dir}" ]; then
        make_dir ${install_dir}
        install_path_uid=$(stat -c "%u" "${install_dir}/../..")
        if [ "${install_path_uid}" != "${cur_uid}" ]; then
            print "ERROR" "Install failed, install dir uid and current user uid mismatch"
            exit 1
        fi
        if [ ! -d "${install_dir}" ]; then
            print "ERROR" "Install failed, create ${install_dir} faied"
            exit 1
        fi
    else
        install_path_uid=$(stat -c "%u" "${install_dir}")
        if [ "${install_path_uid}" != "${cur_uid}" ]; then
            print "ERROR" "Install failed, install dir uid and current user uid mismatch"
            exit 1
        fi
    fi
}

function install_python_api() {
    cd $install_dir
    benchmark_wheel_path=$(find $install_dir/bin/ -name mindiebenchmark*.whl)
    client_wheel_path=$(find $install_dir/bin/ -name mindieclient*.whl)
    model_wrapper_path=$(find $install_dir/bin/ -name model_wrapper*.whl)
    om_adapter_path=$(find $install_dir/bin/ -name om_adapter*.whl)
    node_manager_wheel_path=$(find $install_dir/bin/ -name node_manager*.whl)
    print "INFO" "Ready to start install ${benchmark_wheel_path}, ${client_wheel_path}, \
    ${infer_engine_path}, ${model_wrapper_path}, ${om_adapter_path}, ${node_manager_wheel_path}"

    python_version=$(python3 -c 'import sys; print(sys.version_info.major, sys.version_info.minor)' 2>&1)
    # Get major and minor version number
    py_major_minor=$(echo $python_version | awk '{print $1"."$2}')

    py_cmd=""
    if [[ "$py_major_minor" == "3.11" ]]; then
        py_cmd="python3.11"
    elif [[ "$py_major_minor" == "3.10" ]]; then
        py_cmd="python3.10"
    elif [[ "$py_major_minor" == "3.9" ]]; then
        py_cmd="python3.9"
    fi

    if [[ -n "${py_cmd}" ]]; then
        # Install new version
        ${py_cmd} -m pip install ${benchmark_wheel_path} --log-file ${log_file} --force-reinstall
        if [ $? -ne 0 ]; then
            print "ERROR" "Failed to install benchmark wheel for mindie service"
            exit 1
        fi
        ${py_cmd} -m pip install ${client_wheel_path} --log-file ${log_file} --force-reinstall
        if [ $? -ne 0 ]; then
            print "ERROR" "Failed to install client wheel for mindie service"
            exit 1
        fi
        ${py_cmd} -m pip install ${om_adapter_path} --log-file ${log_file} --force-reinstall
        if [ $? -ne 0 ]; then
            print "ERROR" "Failed to install adapter wheel for mindie service"
            exit 1
        fi
        ${py_cmd} -m pip install ${node_manager_wheel_path} --log-file ${log_file} --force-reinstall
        if [ $? -ne 0 ]; then
            print "ERROR" "Failed to install node_manager wheel for mindie service"
            exit 1
        fi
        print "INFO" "mindiebenchmark, mindieclient, infer_engine, model_wrapper and tokenizer python api Install finish!"
    else
        # Only support python3.9, python3.10 and python3.11
        print "ERROR" "MindIE-service python api Install failed, please install python3.11 ~ python3.9 firstly!"
        exit 1
    fi
}

function install_to_path() {
    install_dir=${default_install_path}/$VERSION
    [ -n "${install_dir}" ] && rm -rf $install_dir
    check_path
    mv ${sourcedir}/* $install_dir
    cd ${install_dir}
    rm -rf install.sh
    install_python_api
    cd ${default_install_path}
    if [ -f "$install_dir/scripts/set_env.sh" ]; then
        rm -rf ${default_install_path}/set_env.sh;
        [ "$install_path_flag" == "y" ] && sed -i "s|/usr/local/Ascend/mindie-service|$default_install_path|g" $install_dir/scripts/set_env.sh
        mv $install_dir/scripts/set_env.sh ${default_install_path}
    fi
    if [ -d $VERSION ]; then
        ln -snf $VERSION latest
    else
        print "ERROR" "Directory $install_dir not exist."
    fi
}

function install_process() {
    if [ -n "${target_dir}" ]; then
        if [[ ! "${target_dir}" = /* ]]; then
            print "ERROR" "Install failed, [ERROR] use absolute path for --install-path argument"
            exit 1
        fi
        install_to_path
        if [ $? -eq 0 ]; then
            print "INFO" "Finish install run for mindie_service."
            [ -f "${default_install_path}/set_env.sh" ] && print "INFO" "Remember to source atb/set_env.sh and atb-models/set_env.sh before source our set_env.sh."
        fi
    else
        install_to_path
        if [ $? -eq 0 ]; then
            print "INFO" "Finish install run for mindie_service."
            [ -f "${default_install_path}/set_env.sh" ] && print "INFO" "Remember to source atb/set_env.sh and atb-models/set_env.sh before source our set_env.sh."
        fi
    fi
}

function check_owner() {
    local cur_owner=$(whoami)

    if [ "${ASCEND_HOME_PATH}" == "" ]; then
        print "ERROR" "Install failed, please source cann set_env.sh first."
        exit 1
    else
        cann_path=${ASCEND_HOME_PATH}
    fi

    if [ ! -d "${cann_path}" ]; then
        print "ERROR" "Install failed, can not find cann in ${cann_path}."
        exit 1
    fi

    cann_owner=$(stat -c %U "${cann_path}")

    if [ "${cann_owner}" != "${cur_owner}" ]; then
        print "ERROR" "Install failed, current owner is not same with CANN."
        exit 1
    fi

    if [[ "${cur_owner}" != "root" ]]; then
        default_install_path="/home/${cur_owner}/Ascend/mindie-service"
        py_install_path=$(python3 -c "import site; print(site.getusersitepackages())")
    fi

    if [ "${install_path_flag}" == "y" ]; then
        default_install_path="${target_dir}"
    fi

    print "INFO" "check owner pass"
}

function check_arch() {
    cur_env_arch=$(uname -m)
    if [ "${cur_env_arch}" != "$ARCH" ]; then
        print "ERROR" "It is system of ${cur_env_arch}, but run package is ${ARCH}"
        exit 1
    fi
}

function main() {
    parse_script_args $*
    if [[ "${install_path_flag}" == "y" || "${install_flag}" == "y" ]]; then
        log_init
        check_arch
        check_owner
        install_process
        chmod_authority
    fi
}

main $*