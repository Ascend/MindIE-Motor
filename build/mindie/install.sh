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

[ -z "$GLOBAL_ABI_VERSION" ] && GLOBAL_ABI_VERSION="0"
export GLOBAL_ABI_VERSION
echo "GLOBAL_ABI_VERSION: ${GLOBAL_ABI_VERSION}"

install_flag=n
uninstall_flag=n
upgrade_flag=n
install_path_flag=n
quiet_flag=n
install_dir=""
temp_dir=""
sourcedir=$PWD
target_dir=""
ABI=$GLOBAL_ABI_VERSION
default_install_path="/usr/local/Ascend/mindie"
VERSION=replace_version
LOG_PATH=replace_log_path
LOG_NAME=replace_log_name
ARCH=replace_arch

declare -A param_dict=()

if [ "$UID" = "0" ]; then
    log_file=${LOG_PATH}${LOG_NAME}
else
    cur_owner=$(whoami)
    LOG_PATH="/home/${cur_owner}${LOG_PATH}"
    log_file=${LOG_PATH}${LOG_NAME}
fi

function log() {
    if [ "x$log_file" = "x" ] || [ ! -f "$log_file" ]; then
        echo -e "[mindie] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2"
    else
        echo -e "[mindie] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2" >>$log_file
    fi
}

# Log to file and screen
function print() {
    if [ "x$log_file" = "x" ] || [ ! -f "$log_file" ]; then
        echo -e "[mindie] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2"
    else
        echo -e "[mindie] [$(date +%Y%m%d-%H:%M:%S)] [$1] $2" | tee -a $log_file
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

## Log init module ##
function log_init() {
    # Check if log path exist, create it if not
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
            local log_file_move_name="ascend_mindie_install_bak.log"
            mv -f ${log_file} ${LOG_PATH}${log_file_move_name}
            chmod 440 ${LOG_PATH}${log_file_move_name}
            make_file "$log_file"
            chmod 640 ${log_file}
            log "INFO" "log file > 50M, move ${log_file} to ${LOG_PATH}${log_file_move_name}."
        fi
    fi
    print "INFO" "Log save in ${log_file}"
}

function chmod_authority() {
    chmod 750 ${default_install_path}
    chmod 750 ${install_dir}

    chmod_dir ${LOG_PATH} "750"
    chmod_file ${log_file}

    chmod_dir ${install_dir}/scripts "550"
    chmod 550 ${install_dir}/scripts/uninstall.sh
    chmod 440 ${install_dir}/version.info
}

function chmod_file() {
    chmod_recursion ${1} "640" "file" "*.log"
}

function chmod_dir() {
    chmod_recursion ${1} ${2} "dir"
}

function chmod_recursion() {
    local rights=$2
    if [ "$3" = "dir" ]; then
        find $1 -type d -exec chmod ${rights} {} \; 2>/dev/null
    elif [ "$3" = "file" ]; then
        find $1 -type f -name $4 -exec chmod ${rights} {} \; 2>/dev/null
    fi
}

function parse_script_args() {
    local num=0
    while true; do
        if [[ x"$1" == x"" ]]; then
            break
            fi
        if [[ "$(expr substr $1 1 2)" == "--" ]]; then
            num=$(expr $num + 1)
        fi
        if [[ $num -gt 2 ]]; then
            break
        fi
        shift 1
    done
    while true
    do
        case "$1" in
        --quiet)
            quiet_flag=y
            let param_dict["quiet"]++
            shift
            ;;
        --install)
            install_flag=y
            let param_dict["install"]++
            shift
            ;;
        --install-path=*)
            install_path_flag=y
            target_dir=$(echo $1 | cut -d"=" -f2-)
            target_dir=${target_dir}/mindie
            let param_dict["install-path"]++
            shift
            ;;
        --uninstall)
            uninstall_flag=y
            let param_dict["uninstall"]++
            shift
            ;;
        --upgrade)
            upgrade_flag=y
            let param_dict["upgrade"]++
            shift
            ;;
        --*)
            shift
            ;;
        *)
            if [ "x$1" != "x" ]; then
                print "ERROR" "Unsupported parameters: $1"
                print "Please input --help for more help"
                exit 1
            fi
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
        default_install_path="/home/${cur_owner}/Ascend/mindie"
    fi

    if [ "${install_path_flag}" == "y" ]; then
        default_install_path="${target_dir}"
    fi

    print "INFO" "check owner pass"
}

function check_path() {
    if [ ! -d "${install_dir}" ]; then
        mkdir -p ${install_dir}
        if [ ! -d "${install_dir}" ]; then
            print "ERROR" "Install failed, [ERROR] create ${install_dir} faied"
            exit 1
        fi
    fi
    if [ ! -d "${temp_dir}" ]; then
        mkdir -p ${temp_dir}
        if [ ! -d "${temp_dir}" ]; then
            print "ERROR" "Install failed, [ERROR] create ${temp_dir} faied"
            exit 1
        fi
    fi
}

function install_failed_process() {
    print "INFO" "clear install dir!"
    [ -n "${default_install_path}/temp" ] && chmod 750 -R "${default_install_path}/temp" && rm -rf ${default_install_path}/temp
    [ -n "${default_install_path}/${VERSION}" ] && chmod 750 -R "${default_install_path}/${VERSION}" && rm -rf ${default_install_path}/${VERSION}
    if [ -h "${default_install_path}/latest" ]; then
        rm -f ${default_install_path}/latest
    fi
    if [ -f "${default_install_path}/set_env.sh" ]; then
        chmod 750 "${default_install_path}/set_env.sh"
        rm -f ${default_install_path}/set_env.sh
    fi
    if [ -n "${default_install_path}" ] && [ ! "$(ls -A ${default_install_path})" ]; then
        chmod 750 "${default_install_path}"
        rm -rf ${default_install_path}
    fi
}

function get_python_version() {
    py_version=$(python3 -c 'import sys; print(sys.version_info[0], ".", sys.version_info[1])' | tr -d ' ')
    py_major_version=${py_version%%.*}
    py_minor_version=${py_version##*.}
}

function install_mindie_rt() {
    if ! ls ./Ascend-mindie-rt*abi${ABI}.run 1> /dev/null 2>&1; then
        print "WARN" "Ascend-mindie-rt*abi${ABI}.run not exist, skip to install it!"
        return
    fi
    get_python_version
    if [[ "$py_major_version" == "3" ]] && { [[ "$py_minor_version" == "10" ]] || [[ "$py_minor_version" == "11" ]]; }; then
        python_interpreter="python3.$py_minor_version"
        print "INFO" "Current Python Interpreter: ${python_interpreter}"
    else
        print "ERROR" "MindIE-RT install failed, please install Python3.10 or Python3.11 first"
        install_failed_process
        exit 1
    fi

    bash $(ls ./Ascend-mindie-rt*abi${ABI}.run  | grep "py${py_major_version}${py_minor_version}") --install-path=$temp_dir
    if [ $? -ne 0 ]; then
        print "ERROR" "mindie-rt Install failed"
        install_failed_process
        exit 1
    fi
    mindie_rt_install_dir=${install_dir}/mindie-rt/

    [ -n "${mindie_rt_install_dir}" ] && chmod_dir ${mindie_rt_install_dir} "750"
    [ -n "${mindie_rt_install_dir}" ] && rm -rf $mindie_rt_install_dir

    mkdir -p ${mindie_rt_install_dir}
    cp -r -f ${temp_dir}/mindie-rt/latest/* ${mindie_rt_install_dir}
    cp -r -f ${temp_dir}/mindie-rt/set_env.sh ${mindie_rt_install_dir}
    sed -i "s!/latest!!" ${mindie_rt_install_dir}/set_env.sh
    chmod_dir ${mindie_rt_install_dir}/scripts "750"
    [ -n "${mindie_rt_install_dir}/scripts" ] && rm -rf ${mindie_rt_install_dir}/scripts
    chmod 750 $mindie_rt_install_dir
    print "INFO" "mindie-rt Install SUCCESS in ${mindie_rt_install_dir}/"
}

function install_mindie_torch() {
    if ! ls ./Ascend-mindie-torch*.tar.gz 1> /dev/null 2>&1; then
        print "WARN" "Ascend-mindie-torch*.tar.gz not exist, skip to install it!"
        return
    fi
    tar -xf Ascend-mindie-torch*.tar.gz
    cd Ascend-mindie-torch*/
    bash MindIETorch*abi${ABI}*.run --install-path=$temp_dir
    if [ $? -ne 0 ]; then
        print "ERROR" "mindie-torch Install failed"
        install_failed_process
        exit 1
    fi
    mindie_torch_install_dir=${install_dir}/mindie-torch

    [ -n "${mindie_torch_install_dir}" ] && chmod_dir ${mindie_torch_install_dir} "750"
    [ -n "${mindie_torch_install_dir}" ] && rm -rf $mindie_torch_install_dir

    mkdir -p ${mindie_torch_install_dir}
    cp -r -f ${temp_dir}/mindietorch/latest/* ${mindie_torch_install_dir}
    cp -r -f ${temp_dir}/mindietorch/set_env.sh ${mindie_torch_install_dir}
    sed -i "s!/latest!!" ${mindie_torch_install_dir}/set_env.sh
    chmod_dir ${mindie_torch_install_dir}/scripts "750"
    [ -n "${mindie_torch_install_dir}/scripts" ] && rm -rf ${mindie_torch_install_dir}/scripts
    chmod 750 $mindie_torch_install_dir

    get_python_version
    if [[ "$py_major_version" == "3" ]] && { [[ "$py_minor_version" == "10" ]] || [[ "$py_minor_version" == "11" ]]; }; then
        python_interpreter="python3.$py_minor_version"
        print "INFO" "Current Python Interpreter: ${python_interpreter}"
    else
        print "ERROR" "MindIE-Torch python api install failed, please install Python3.10 or Python3.11 first"
        install_failed_process
        exit 1
    fi

    ${python_interpreter} -m pip install $(ls ./mindietorch*.whl | grep "cp${py_major_version}${py_minor_version}") --log-file ${log_file} --force-reinstall
    if test $? -ne 0; then
        print "ERROR" "Install mindie-torch whl package failed, detail info can be checked in ${log_file}."
        install_failed_process
        exit 1
    else
        print "INFO" "Install mindie-torch whl package success."
    fi

    print "INFO" "mindie-torch Install SUCCESS in ${mindie_torch_install_dir}!"
    cd ..
}

function install_mindie_sd() {
    if ! ls ./Ascend-mindie-sd*.tar.gz 1> /dev/null 2>&1; then
        print "WARN" "Ascend-mindie-sd*.tar.gz not exist, skip to install it!"
        return
    fi
    tar -xf Ascend-mindie-sd*.tar.gz
    cd Ascend-mindie-sd*/

    get_python_version
    if [[ "$py_major_version" == "3" ]] && { [[ "$py_minor_version" == "10" ]] || [[ "$py_minor_version" == "11" ]]; }; then
        python_interpreter="python3.$py_minor_version"
        print "INFO" "Current Python Interpreter: ${python_interpreter}"
    else
        print "ERROR" "MindIE-SD python api install failed, please install Python3.10 or Python3.11 first"
        install_failed_process
        exit 1
    fi

    ${python_interpreter} -m pip install ./mindiesd*.whl --log-file ${log_file} --force-reinstall
    if test $? -ne 0; then
        print "ERROR" "Install mindie-sd whl package failed, detail info can be checked in ${log_file}."
        install_failed_process
        exit 1
    else
        print "INFO" "Install mindie-sd whl package success."
    fi

    print "INFO" "mindie-sd whl package Install SUCCESS!"
    cd ..
}

function install_mindie_service() {
    if ! ls ./Ascend-mindie-service*.run 1> /dev/null 2>&1; then
        print "WARN" "Ascend mindie-service not exist, skip to install it!"
        return
    fi
    get_python_version
    bash $(ls ./Ascend-mindie-service*.run  | grep "py${py_major_version}${py_minor_version}") --install-path=$temp_dir
    if [ $? -ne 0 ]; then
        print "ERROR" "mindie-service Install failed"
        install_failed_process
        exit 1
    fi
    mindie_service_install_dir=${install_dir}/mindie-service
    mkdir -p ${mindie_service_install_dir}
    chmod_dir ${mindie_service_install_dir} "750"

    if [ "${upgrade_flag}" == "y" ]; then
        upgrade_info_path=${temp_dir}/mindie-service/latest/scripts/utils/upgrade_info.json
        old_config_file=${default_install_path}/latest/mindie-service/conf/config.json
        old_version_file=${default_install_path}/latest/version.info
        new_config_file=${temp_dir}/mindie-service/latest/conf/config.json
        new_version_file=${temp_dir}/mindie-service/latest/version.info
        if [ -f "$upgrade_info_path" ] && [ -f "$old_config_file" ] && [ -f "$old_version_file" ] && \
            [ -f "$new_config_file" ] && [ -f "$new_version_file" ]; then
            old_version=$(grep "mindie-service" "$old_version_file" | awk -F': ' '{print $2}')
            new_version=$(grep "mindie-service" "$new_version_file" | awk -F': ' '{print $2}')
            print "INFO" "mindie-service config.json retaining start."
            ${python_interpreter} ${temp_dir}/mindie-service/latest/scripts/utils/upgrade_server.py --upgrade_info_path "$upgrade_info_path" \
                --old_config_path "$old_config_file" --old_version "$old_version" --new_config_path "$new_config_file" --new_version "$new_version"
            if test $? -ne 0; then
                print "WARN" "mindie-service config.json retained failed, you can modify it manually"
            else
                print "INFO" "mindie-service config.json retained success."
            fi
        else
            print "WARN" "mindie-service config.json retained failed, you can modify it manually"
        fi
    fi

    cp -r -f ${temp_dir}/mindie-service/latest/* ${mindie_service_install_dir}
    original_permissions=$(stat -c "%a" "$mindie_service_install_dir/scripts")
    chmod 750 ${mindie_service_install_dir}/scripts
    service_uninstall_file=${mindie_service_install_dir}/scripts/uninstall.sh
    chmod 750 ${service_uninstall_file}
    [ -f "${service_uninstall_file}" ] && rm -rf ${service_uninstall_file}
    chmod $original_permissions ${mindie_service_install_dir}/scripts
    chmod_dir ${mindie_service_install_dir} "550"
    chmod -R 700 ${mindie_service_install_dir}/security
    chmod 750 ${mindie_service_install_dir}/logs
    chmod 750 $mindie_service_install_dir
    print "INFO" "mindie-service Install SUCCESS in ${mindie_service_install_dir}"
}

function install_mindie_llm() {
    if ! ls ./Ascend-mindie-llm*.run 1> /dev/null 2>&1; then
        print "WARN" "Ascend mindie-llm not exist, skip to install it!"
        return
    fi
    get_python_version
    bash $(ls ./Ascend-mindie-llm*.run  | grep "py${py_major_version}${py_minor_version}") --install-path=$temp_dir
    if [ $? -ne 0 ]; then
        print "ERROR" "mindie-llm Install failed"
        install_failed_process
        exit 1
    fi
    mindie_llm_install_dir=${install_dir}/mindie-llm

    [ -n "${mindie_llm_install_dir}" ] && chmod_dir ${mindie_llm_install_dir} "750"
    [ -n "${mindie_llm_install_dir}" ] && rm -rf $mindie_llm_install_dir

    mkdir -p ${mindie_llm_install_dir}
    # cp mindie-llm lib to mindie-service folder
    mindie_service_install_dir=${install_dir}/mindie-service
    cp -r -f ${temp_dir}/mindie_llm/latest/lib/libconfig_manager.so ${mindie_service_install_dir}/lib
    cp -r -f ${temp_dir}/mindie_llm/latest/lib/libmindieservice_tokenizer.so ${mindie_service_install_dir}/lib
    cp -r -f ${temp_dir}/mindie_llm/latest/lib/libmindieservice_common.so ${mindie_service_install_dir}/lib
    cp -r -f ${temp_dir}/mindie_llm/latest/lib/libmindieservice_endpoint.so ${mindie_service_install_dir}/lib
    cp -r -f ${temp_dir}/mindie_llm/latest/bin/mindieservice_daemon ${mindie_service_install_dir}/bin
    cp -r -f ${temp_dir}/mindie_llm/latest/conf/config.json ${mindie_service_install_dir}/examples/kubernetes_deploy_scripts/conf/config.json
    cp -r -f ${temp_dir}/mindie_llm/latest/conf/config.json ${mindie_service_install_dir}/examples/kubernetes_deploy_scripts/conf/config_p.json
    cp -r -f ${temp_dir}/mindie_llm/latest/conf/config.json ${mindie_service_install_dir}/examples/kubernetes_deploy_scripts/conf/config_d.json
    cp -r -f ${temp_dir}/mindie_llm/latest/conf/config.json ${mindie_service_install_dir}/conf
    rm -rf ${temp_dir}/mindie_llm/latest/conf
    chmod 640 ${mindie_service_install_dir}/examples/kubernetes_deploy_scripts/conf/*.json
    chmod 640 ${mindie_service_install_dir}/conf/config.json
    cp -r -f ${temp_dir}/mindie_llm/latest/server/scripts/* ${mindie_service_install_dir}/scripts
    cp -r -f ${temp_dir}/mindie_llm/latest/server/scripts/set_env.sh ${mindie_service_install_dir}
    rm -rf ${temp_dir}/mindie_llm/latest/server
    sed -i "s!/latest!!" ${mindie_service_install_dir}/set_env.sh
    cp -r -f ${temp_dir}/mindie_llm/latest/* ${mindie_llm_install_dir}
    cp -r -f ${temp_dir}/mindie_llm/set_env.sh ${mindie_llm_install_dir}
    sed -i "s!/latest!!" ${mindie_llm_install_dir}/set_env.sh
    chmod_dir ${mindie_llm_install_dir}/scripts "750"
    [ -n "${mindie_llm_install_dir}/scripts" ] && rm -rf ${mindie_llm_install_dir}/scripts
    chmod 750 $mindie_llm_install_dir
    print "INFO" "mindie-llm Install SUCCESS in ${mindie_llm_install_dir}"
}

function install_sub_run() {
    install_mindie_rt
    install_mindie_torch
    install_mindie_service
    install_mindie_llm
    install_mindie_sd

    [ -f "${default_install_path}/set_env.sh" ] && rm -rf ${default_install_path}/set_env.sh
    mv -f set_env.sh $default_install_path
    mkdir -p ${install_dir}/scripts
    chmod 750 ${install_dir}/scripts
    mv -f scripts/uninstall.sh ${install_dir}/scripts
    chmod 550 ${install_dir}/scripts
    mv -f version.info ${install_dir}
    chmod_dir ${temp_dir} "750"
    [ -n "${temp_dir}" ] && rm -rf ${temp_dir}
}

# Normally install
function install_to_path() {
    install_dir=${default_install_path}/${VERSION}
    temp_dir=${default_install_path}/temp

    if [ -n "${default_install_path}" ] && [ -d "${default_install_path}" ]; then
        chmod 750 "${default_install_path}"
    fi

    if [ "${install_flag}" == "y" ]; then
        [ -n "${install_dir}" ] && chmod_dir ${install_dir} "750"
        [ -n "${install_dir}" ] && rm -rf $install_dir
    fi
    [ -n "${temp_dir}" ] && chmod_dir ${temp_dir} "750"
    [ -n "${temp_dir}" ] && rm -rf $temp_dir
    check_path
    install_sub_run
    cd ${default_install_path}

    if [ "${install_flag}" == "y" ] || [ "${upgrade_flag}" == "y" ]; then
        if [ -d $VERSION ]; then
            ln -snf $VERSION latest
        else
            print "ERROR" "Directory $install_dir not exist."
        fi
    fi
}

function install_process() {
    if [ -n "${target_dir}" ]; then
        if [[ ! "${target_dir}" = /* ]]; then
            print "ERROR" "Install failed, [ERROR] use absolute path for --install-path argument"
            exit 1
        fi
        install_to_path
    else
        install_to_path
    fi
}

function upgrade_process() {
    if [ -n "${target_dir}" ]; then
        if [[ ! "${target_dir}" = /* ]]; then
            print "ERROR" "Install failed, [ERROR] use absolute path for --install-path argument"
            exit 1
        fi
    fi
    cur_install_path=${default_install_path}/${VERSION}
    if [ ! -d "${cur_install_path}" ]; then
        print "INFO" "The Mindie version ${VERSION} does not exist, installing Mindie version ${VERSION} !"
    fi
    install_to_path
    print "INFO" "Finish upgrade run for Mindie package."
}

function uninstall_process() {
    print "INFO" "uninstall start"
    reinstall_check
    if [ $? -eq 1 ]; then
        print "ERROR" "run package is not install on path ${default_install_path}, uninstall failed !"
        print "ERROR" "check the environment failed"
        log "ERRRO" "process exit"
        exit 2
    fi
    deal_uninstall
}

function check_arch() {
    cur_env_arch=$(uname -m)
    if [ "${cur_env_arch}" != "$ARCH" ]; then
        print "ERROR" "It is system of ${cur_env_arch}, but run package is ${ARCH}"
        exit 1
    fi
}

function handle_eula() {
    if echo "${LANG}" | grep -q "zh_CN"; then
        eula_file=./eula_cn.txt
    else
        eula_file=./eula_en.txt
    fi
    print "INFO" "show ${eula_file}"
    cat "${eula_file}" 1>&2
    read -n1 -re -p "Do you accept the EULA to $1 MindIE?[Y/N]" answer
    case $answer in
        Y|y)
            print "INFO" "Accept EULA, start to $1"
            ;;
        *)
            print "ERROR" "Reject EULA, quit to $1"
            exit 1
            ;;
    esac
}

# Check if the package is installed
function reinstall_check() {
    if [ -f ${default_install_path}/latest/version.info ]; then
        return 0
    else
        return 1
    fi
}

function deal_uninstall() {
    ${default_install_path}/latest/scripts/uninstall.sh
}

function check_script_args() {
    # Check repeated parameters
    for key in "${!param_dict[@]}"; do
        if [ ${param_dict[$key]} -gt 1 ]; then
            print "ERROR" "parameter error ! $key is repeat."
            exit 1
        fi
    done
    # Check necessary parameters
    local args_num=0
    if [ x${param_dict["install"]} == x1 ]; then
        let 'args_num+=1'
    fi
    if [ x${param_dict["upgrade"]} == x1 ]; then
        let 'args_num+=1'
    fi
    if [ x${param_dict["uninstall"]} == x1 ]; then
        let 'args_num+=1'
    fi
    if [ $args_num -gt 1 ]; then
        print "ERROR" "parameter error | Scene conflict."
        exit 1
    fi

    if [ "$install_path_flag" = "y" ] && [ "$uninstall_flag" != "y" ] &&
        [ "$upgrade_flag" != "y" ] && [ "$install_flag" != "y" ]; then
        print 'ERROR' 'the "--install-path" parameter cannot be used alone. It must be used with either "--install",
            "--uninstall" or "--upgrade", please use "--help" or "-h" option for detailed information.'
        exit 1
    fi

    # Only `install-path` and `quiet` are allowed for upgrade and uninstall process.
    if [ "$upgrade_flag" = "y" -o "$uninstall_flag" = "y" ]; then
        local param_num=${#param_dict[@]}
        if [ x${param_dict["install-path"]} != x"" ]; then
            let 'param_num-=1'
        fi
        if [ x${param_dict["quiet"]} != x"" ]; then
            let 'param_num-=1'
        fi
        if [ ${param_num} -gt 1 ]; then
            print "ERROR" "upgrade/uninstall scene does not supported other parameter."
            exit 1
        fi
    fi
}

function main() {
    parse_script_args $*
    check_script_args
    if [ "${install_flag}" == "n" -a "${upgrade_flag}" == "n" -a "${uninstall_flag}" == "n" ]; then
        exit 0
    fi
    log_init
    check_arch
    check_owner
    if [[ "${install_flag}" == "y" ]]; then
        if [ "${quiet_flag}" == "n" ]; then
            handle_eula "install"
        else
            print "INFO" "Quiet install, accept EULA by default"
        fi
        install_process
        chmod_authority
        print "INFO" "mindie Install SUCCESS in ${install_dir}"
        echo "------"
        echo "To take effect for current user, you can exec command : source ${default_install_path}/set_env.sh"
        echo "------"
    elif [[ "${upgrade_flag}" == "y" ]]; then
        if [ "${quiet_flag}" == "n" ]; then
            handle_eula "upgrade"
        else
            print "INFO" "Quiet upgrade, accept EULA by default"
        fi
        upgrade_process
        chmod_authority
        print "INFO" "mindie Upgrade SUCCESS in ${install_dir}"
        echo "------"
        echo "To take effect for current user, you can exec command : source ${default_install_path}/set_env.sh"
        echo "------"
    elif [[ "${uninstall_flag}" == "y" ]]; then
        uninstall_process
    fi
}

main $*
