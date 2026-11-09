#!/usr/bin/env bash
# 应用 snapshot 或 fault_tolerance patch, 建议先 FT 再 snapshot.
# 用法: PYTHON=/path/to/python3 bash apply_patches.sh <vllm_patch> <vllm_ascend_patch>
# 默认使用 python3. 通过 pip 定位安装目录, 不 import 包或初始化 NPU.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "用法: $0 <vllm_patch> <vllm_ascend_patch>" >&2
    exit 1
fi

PYTHON=${PYTHON:-python3}
patches=("$(realpath -e "$1")" "$(realpath -e "$2")")
names=(vllm vllm-ascend)
modules=(vllm vllm_ascend)
roots=()
actions=()

resolve_package_root() {
    local info editable location version
    info=$("$PYTHON" -m pip show "$1")
    version=$(awk -F': ' '/^Version:/{print $2}' <<< "$info")
    # stdout 仅返回包目录, 日志写入 stderr, 避免混入调用方的 root 变量.
    echo ">>> 已安装包版本 ($1): ${version:-未记录}" >&2
    editable=$(awk -F': ' '/^Editable project location:/{print $2}' <<< "$info")
    location=$(awk -F': ' '/^Location:/{print $2}' <<< "$info")
    if [ -n "$editable" ] && [ -d "$editable/$2" ]; then
        realpath "$editable"
    elif [ -n "$location" ] && [ -d "$location/$2" ]; then
        realpath "$location"
    else
        echo "错误: 无法通过 $PYTHON 定位 $1, 请设置正确的 PYTHON." >&2
        return 1
    fi
}

apply_package_patch() {
    local root="$1"
    shift
    # 阻止 Git 发现安装目录之外的仓库, 避免从子目录应用时静默跳过补丁.
    GIT_CEILING_DIRECTORIES="$(dirname "$root")" git -C "$root" apply "$@"
}

# 两个包均预检通过后再应用. 期间不要并发修改包文件.
# 不自动转换 CRLF, 不使用 --reject 或强制应用冲突补丁.
for i in 0 1; do
    echo ">>> 补丁文件: ${patches[$i]}"
    root=$(resolve_package_root "${names[$i]}" "${modules[$i]}")
    roots+=("$root")
    echo ">>> 检查 ${names[$i]}: $root"
    if apply_package_patch "$root" --check --ignore-whitespace "${patches[$i]}" 2>/dev/null; then
        actions+=(apply)
    elif apply_package_patch "$root" --reverse --check --ignore-whitespace "${patches[$i]}" 2>/dev/null; then
        actions+=(skip)
        echo ">>> 当前补丁已应用, 跳过"
    else
        apply_package_patch "$root" --check --ignore-whitespace "${patches[$i]}" >&2
        exit 1
    fi
done

# 不是跨包事务, 实际写入时的 IO 失败不会自动回滚.
for i in 0 1; do
    [ "${actions[$i]}" = apply ] || continue
    apply_package_patch "${roots[$i]}" --ignore-whitespace "${patches[$i]}"
    echo ">>> ${names[$i]}: 应用成功"
done
