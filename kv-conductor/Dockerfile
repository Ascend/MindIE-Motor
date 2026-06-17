# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
#
# Build:  cargo build --release && docker build -t kv-conductor .
# Run:    docker run -p 13333:13333 kv-conductor

FROM ubuntu:24.04

# libzmq runtime for ZMQ SUB feature (optional, harmless when not used)
RUN apt-get update && apt-get install -y --no-install-recommends libzmq5 && \
    rm -rf /var/lib/apt/lists/*

COPY target/release/kv-conductor /usr/local/bin/kv-conductor

EXPOSE 13333

ENTRYPOINT ["kv-conductor"]
CMD ["--port", "13333"]
