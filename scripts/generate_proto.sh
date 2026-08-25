#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# Script to generate Python code from .proto files
# This script generates _pb2.py and _pb2_grpc.py files from .proto files
#
# Example .proto file structure (motor/common/etcd/proto/kv.proto):
#   syntax = "proto3";
#   package motor.common.etcd.proto;
#
#   message KVPair {
#     string key = 1;
#     string value = 2;
#   }
#
#   service KVService {
#     rpc Get(KVPair) returns (KVPair);
#     rpc Put(KVPair) returns (KVPair);
#   }

set -e  # Exit on error

# Get the project root directory
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Check if grpcio-tools is installed
if ! python -c "import grpc_tools.protoc" 2>/dev/null; then
    echo "Error: grpcio-tools is not installed."
    echo "Please install it with: pip install 'grpcio-tools>=1.82.1' 'protobuf>=7.35.1,<8.0.0'"
    exit 1
fi

# Find all .proto files (excluding venv and other common virtual environment directories)
PROTO_FILES=$(find . -type d \( -name "venv" -o -name ".venv" -o -name "env" -o -name ".env" -o -name "__pycache__" -o -name "node_modules" \) -prune -o -name "*.proto" -type f -print)

if [ -z "$PROTO_FILES" ]; then
    echo "No .proto files found."
    exit 0
fi

# Generate Python code for each .proto file
for proto_file in $PROTO_FILES; do
    echo "Generating code from $proto_file..."

    # Get the directory containing the .proto file
    proto_dir=$(dirname "$proto_file")

    # Get the base name of the .proto file (without extension)
    proto_base=$(basename "$proto_file" .proto)

    # Generate _pb2.py and _pb2_grpc.py files
    # Change to proto directory for protoc execution (protoc requires proto_path to match file location)
    cd "$proto_dir"
    python -m grpc_tools.protoc \
        --proto_path="." \
        --python_out="." \
        --grpc_python_out="." \
        "$(basename "$proto_file")"
    cd "$ROOT_DIR"

    if [ $? -eq 0 ]; then
        echo "✓ Successfully generated code from $proto_file"

        # Fix import paths in _pb2_grpc.py if it exists
        pb2_grpc_file="${proto_dir}/${proto_base}_pb2_grpc.py"
        if [ -f "$pb2_grpc_file" ]; then
            # Get the Python package path (e.g., motor/common/etcd/proto/kv.proto -> motor.common.etcd.proto)
            # Remove leading ./ and .proto extension, get directory path, then convert / to .
            proto_rel_path=$(echo "$proto_file" | sed 's|^\./||' | sed 's|\.proto$||')
            package_path=$(dirname "$proto_rel_path" | sed 's|/|.|g')
            if [ "$package_path" = "." ]; then
                package_path=""
            fi

            # Replace relative import with absolute import
            # Pattern: import kv_pb2 -> from motor.common.etcd.proto import kv_pb2
            # Use sed with word boundary to avoid double replacement
            if [ -n "$package_path" ]; then
                # First check if already replaced to avoid double replacement
                if ! grep -q "^from ${package_path} import ${proto_base}_pb2" "$pb2_grpc_file"; then
                    sed -i "s|^import ${proto_base}_pb2\([^_]\)|from ${package_path} import ${proto_base}_pb2\1|g" "$pb2_grpc_file"
                    sed -i "s|^import ${proto_base}_pb2 as|from ${package_path} import ${proto_base}_pb2 as|g" "$pb2_grpc_file"
                fi
            fi

            echo "  Fixed import paths in ${proto_base}_pb2_grpc.py"
        fi

        pb2_file="${proto_dir}/${proto_base}_pb2.py"
        if [ -f "$pb2_file" ]; then
            proto_rel_path=$(echo "$proto_file" | sed 's|^\./||' | sed 's|\.proto$||')
            package_path=$(dirname "$proto_rel_path" | sed 's|/|.|g')
            [ "$package_path" = "." ] && package_path=""

            if [ -n "$package_path" ]; then
                # regex to match 'import xxx_pb2' not followed by more word chars (avoid matching xxx_pb2_extra)
                # Also handle 'as' alias
                if ! grep -q "^from ${package_path} import [a-zA-Z0-9_]*_pb2" "$pb2_file"; then
                    # Replace plain imports: import b_pb2 -> from pkg import b_pb2
                    sed -i "s|^import \([a-zA-Z0-9_]*_pb2\)\([^_a-zA-Z0-9].*\)\?$|from ${package_path} import \1\2|g" "$pb2_file"
                    # Replace aliased imports: import b_pb2 as c -> from pkg import b_pb2 as c
                    sed -i "s|^import \([a-zA-Z0-9_]*_pb2\) as \(.*\)$|from ${package_path} import \1 as \2|g" "$pb2_file"
                fi
            fi
            echo "  Fixed import paths in ${proto_base}_pb2.py"
        fi
    else
        echo "✗ Failed to generate code from $proto_file"
        exit 1
    fi
done

echo "All protobuf files generated successfully."
