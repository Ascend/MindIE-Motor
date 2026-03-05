/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#include <string>
#include "Logger.h"
#include "Util.h"
#include "KmcDecryptor.h"
#include "GrpcTlsFunctionExpansion.h"
#include "GrpcClusterClient.h"

namespace MINDIE::MS {

const int32_t MIN_SENT_PING_INTERVAL_WITHOUT_DATA_MS = 5000;
    
bool GetCertificateProvider(std::shared_ptr<grpc::experimental::CertificateProviderInterface>& certificateProvider,
    const TlsItems& mTlsConfig)
{
    uint32_t mode = mTlsConfig.checkFiles ? 0400 : 0777;  // 证书的权限要求是0400, 不校验是0777
    std::string ca = "";
    if (FileToBuffer(mTlsConfig.caCert, ca, mode, mTlsConfig.checkFiles) != 0) {
        LOG_E("[%s] [ClusterClient] Read CA file failed.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return false;
    }
    std::string cert = "";
    if (FileToBuffer(mTlsConfig.tlsCert, cert, mode, mTlsConfig.checkFiles) != 0) {
        LOG_E("[%s] [ClusterClient] Read TLS certificate file failed.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return false;
    }
    auto kmcDecryptor = KmcDecryptor(mTlsConfig);
    KmcSecureString keySecstr = kmcDecryptor.DecryptPrivateKey();
    if (!keySecstr.IsValid()) {
        LOG_E("[%s] [ClusterClient] Failed to get key file content.",
            GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str());
        return false;
    }
    std::vector<grpc::experimental::IdentityKeyCertPair> pairs{{keySecstr.GetSensitiveInfoContent(), cert}};
    keySecstr.Clear();
    certificateProvider = std::make_shared<grpc::experimental::StaticDataCertificateProvider>(ca, pairs);
    std::fill_n(pairs[0].private_key.begin(), pairs[0].private_key.size(), '\0');
    return true;
}

bool FillClientTlsOpts(std::unique_ptr<grpc::experimental::TlsChannelCredentialsOptions> &tlsOpts,
    const TlsItems& mTlsConfig)
{
    std::vector<std::string> crlPath;
    if (!mTlsConfig.tlsCrl.empty()) {
        crlPath.push_back(mTlsConfig.tlsCrl);
    }
    if (!GrpcTlsFunctionExpansion::CheckTlsOption({mTlsConfig.caCert}, {mTlsConfig.tlsCert}, crlPath,
        mTlsConfig.checkFiles)) {
        LOG_E("[%s] [ClusterClient] Check TLS options failed.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str());
        return false;
    }
    tlsOpts = std::move(std::make_unique<grpc::experimental::TlsChannelCredentialsOptions>());
    std::shared_ptr<grpc::experimental::CertificateProviderInterface> certificateProvider = nullptr;
    if (!GetCertificateProvider(certificateProvider, mTlsConfig)) {
        LOG_E("[%s] [ClusterClient] Get certificate provider failed.",
            GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str());
        return false;
    }
    tlsOpts->set_certificate_provider(certificateProvider);
    tlsOpts->set_root_cert_name(mTlsConfig.caCert);
    tlsOpts->set_identity_cert_name(mTlsConfig.tlsCert);
    tlsOpts->watch_root_certs();
    tlsOpts->watch_identity_key_cert_pairs();
    if (!mTlsConfig.tlsCrl.empty()) {
        std::string crl = "";
        uint32_t mode = mTlsConfig.checkFiles ? 0400 : 0777; // CRL文件的权限要求是0400, 不校验是0777
        if (FileToBuffer(mTlsConfig.tlsCrl, crl, mode, mTlsConfig.checkFiles) != 0) {
            LOG_E("[%s] [ClusterClient] Read CRL file failed: %s",
                GetErrorCode(ErrorType::NOT_FOUND, ControllerFeature::CONTROLLER).c_str(),
                mTlsConfig.tlsCrl.c_str());
            return false;
        }
        std::vector<std::string> crlList{ crl };
        auto crlProviderSpan = grpc_core::experimental::CreateStaticCrlProvider(crlList);
        auto crlProvider = crlProviderSpan.value_or(nullptr);
        if (crlProvider == nullptr) {
            LOG_E("[%s] [ClusterClient] Create CRL provider failed.",
                GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str());
            return false;
        }
        tlsOpts->set_crl_provider(crlProvider);
    }
    return true;
}

std::shared_ptr<grpc::Channel> CreateGrpcChannel(const std::string serverAddr, const TlsItems& mTlsConfig)
{
    grpc::ChannelArguments channelArgs;
    // 每 5 秒发送一次 Ping
    channelArgs.SetInt(GRPC_ARG_KEEPALIVE_TIME_MS, MAX_KEEPALIVE_TIME_MS);
    // 超时时间为 60 秒
    channelArgs.SetInt(GRPC_ARG_KEEPALIVE_TIMEOUT_MS, MAX_KEEPALIVE_TIMEOUT_MS);
    // 允许在没有 RPC 调用时发送 Ping
    channelArgs.SetInt(GRPC_ARG_KEEPALIVE_PERMIT_WITHOUT_CALLS, 1);

    // 允许在没有数据时发送更多 PING
    channelArgs.SetInt(GRPC_ARG_HTTP2_MAX_PINGS_WITHOUT_DATA, 0);  // 0 = 无限制

    // 设置最小 PING 间隔（防止被服务端拒绝）
    channelArgs.SetInt(GRPC_ARG_HTTP2_MIN_SENT_PING_INTERVAL_WITHOUT_DATA_MS,
        MIN_SENT_PING_INTERVAL_WITHOUT_DATA_MS);

    std::shared_ptr<grpc::Channel> retChannel;

    if (!mTlsConfig.tlsEnable) {
        retChannel = grpc::CreateCustomChannel(serverAddr, grpc::InsecureChannelCredentials(), channelArgs);
    } else {
        std::unique_ptr<grpc::experimental::TlsChannelCredentialsOptions> tlsChannelOpts = nullptr;
        if (!FillClientTlsOpts(tlsChannelOpts, mTlsConfig)) {
            LOG_E("[%s] [CreateGrpcChannel] Fill TLS options failed.",
                GetErrorCode(ErrorType::INVALID_INPUT, ControllerFeature::CONTROLLER).c_str());
            return nullptr;
        }
        auto creds = grpc::experimental::TlsCredentials(std::move(*tlsChannelOpts));
        retChannel = grpc::CreateCustomChannel(serverAddr, creds, channelArgs);
    }

    if (retChannel == nullptr) {
        LOG_E("[%s] [CreateGrpcChannel] The created channel is null.",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return nullptr;
    }
    if (!retChannel->WaitForConnected(std::chrono::system_clock::now() + std::chrono::seconds(60))) { // 60:超时时间
        LOG_E("[%s] [CreateGrpcChannel] Channel connection failed(Possibly addr is not reachable or TLS mismatch).",
            GetErrorCode(ErrorType::CALL_ERROR, ControllerFeature::CONTROLLER).c_str());
        return nullptr;
    }
    return retChannel;
}

}