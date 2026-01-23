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
#include <utility>
#include <fstream>
#include <set>
#include <nlohmann/json.hpp>
#include "Logger.h"
#include "Util.h"
#include "HttpClient.h"


namespace MINDIE {
namespace MS {

int32_t HttpClient::Init(const std::string &host, const std::string &port, const TlsItems &tlsConfig, bool useKMC)
{
    this->mHost = host;
    this->mPort = port;
    this->mTlsConfig = tlsConfig;
    this->mUseKMC = useKMC;
    if (this->mTlsConfig.tlsEnable) {
        if (SetTlsCtx() != 0) {
            return -1;
        }
    }
    return 0;
}

void HttpClient::SetHostAndPort(const std::string &host, const std::string &port)
{
    this->mHost = host;
    this->mPort = port;
}

int32_t HttpClient::SendRequest(const Request &request, int timeoutSeconds, int retries, std::string& responseBody,
    int32_t &code)
{
    responseBody.clear();
    int32_t result = 0;
    std::string contentType;
    if (this->mTlsConfig.tlsEnable) {
        result = SendRequestTls(request, timeoutSeconds, retries, responseBody, contentType);
    } else {
        result = SendRequestNonTls(request, timeoutSeconds, retries, responseBody, contentType);
    }
    if (result == -1) {
        LOG_W("[%s] [HttpClient] Failed to send HTTP request.",
            GetErrorCode(ErrorType::CALL_ERROR, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    if (contentType == "application/json") {
        if (!nlohmann::json::accept(responseBody)) {
            LOG_E("[%s] [HttpClient] Receive message is not in a valid JSON format.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPCLIENT).c_str());
            return -1;
        }
    }
    auto subStr = responseBody.substr(0, 256); // 256最大支持的打印长度
    code = result;
    return 0;
}

std::string GetContentType(const Http::response<Http::dynamic_body> &res)
{
    for (auto &header : res.base()) {
        if (header.name() == boost::beast::http::field::content_type) {
            return header.name_string();
        }
    }
    return std::string();
}

struct Endpoint {
    std::string ipAddress;
    std::string port;
};

template<typename Stream>
class Connection : public std::enable_shared_from_this<Connection<Stream>> {
public:
    Connection(Net::io_context &ioc, Stream &stream) : mIoc(ioc), mResolver(ioc), mStream(stream), mTimer(ioc)
    {}

    int32_t Run(Endpoint endpoint, const Request &request,
        uint32_t timeoutSeconds, std::string& responseBody, std::string &contentType)
    {
        LOG_D("[HttpClient] Ready to run http connection.");
        mHost = endpoint.ipAddress;
        mRawReq = request;
        mResolver.async_resolve(endpoint.ipAddress, endpoint.port,
            Beast::bind_front_handler(&Connection<Stream>::OnResolve, this->shared_from_this()));

        SetTimeout(timeoutSeconds);
        mIoc.restart();
        mIoc.run();
        if (mStatus != -1) {
            responseBody = Beast::buffers_to_string(this->mParser.get().body().data());
            contentType = GetContentType(this->mParser.get());
        }
        LOG_D("[HttpClient] finish running http connection.");
        return mStatus;
    }

private:
    void StopConnection()
    {
        mTimer.cancel();
        mIoc.stop();
        return;
    }

    void SetTimeout(uint32_t timeoutSeconds)
    {
        mTimer.expires_after(std::chrono::seconds(timeoutSeconds));
        mTimer.async_wait(Beast::bind_front_handler(&Connection<Stream>::OnTimeout, this->shared_from_this()));
    }

    void OnTimeout(Beast::error_code ec)
    {
        if (ec != Net::error::operation_aborted) {
            LOG_E("[%s] [HttpClient] HTTP client timeout occurred: %s.",
                GetErrorCode(ErrorType::DEADLINE_EXCEEDED, CommonFeature::HTTPCLIENT).c_str(),
                ec.message().c_str());
            Beast::get_lowest_layer(mStream).cancel();
        }
    }

    void OnResolve(Beast::error_code ec, Tcp::resolver::results_type results)
    {
        LOG_D("HTTP client attempting to resolve: %s.", ec.message().c_str());
        if (ec) {
            LOG_E("[%s] [HttpClient] HTTP client failed during resolve operation, reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
                ec.message().c_str());
            return StopConnection();
        }

        // 连接到解析出的IP地址
        Beast::get_lowest_layer(mStream).async_connect(
            results, Beast::bind_front_handler(&Connection<Stream>::OnConnect, this->shared_from_this()));
    }
    void OnConnect(__attribute__((unused)) Beast::error_code ec,
        __attribute__((unused)) Tcp::resolver::results_type::endpoint_type)
    {}
    void OnHandshake(__attribute__((unused)) Beast::error_code ec)
    {}

    void OnWrite(Beast::error_code ec, std::size_t bytesTransferred)
    {
        LOG_D("HTTP client attempting to write: %s.", ec.message().c_str());
        boost::ignore_unused(bytesTransferred);

        if (ec) {
            LOG_E("[%s] [HttpClient] HTTP client failed during write operation, reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
                ec.message().c_str());
            return StopConnection();
        }

        mParser.body_limit(10 * 1024 * 1024); // 10, 1024: 10MB bytes for body
        mParser.header_limit(8 * 1024); // 8, 1024: 8k bytes for header
        // 接收HTTP响应
        Http::async_read(
            mStream, mBuffer,
            mParser,
            Beast::bind_front_handler(&Connection<Stream>::OnRead, this->shared_from_this()));
    }

    void OnRead(__attribute__((unused)) Beast::error_code ec,
        __attribute__((unused)) std::size_t bytesTransferred)
    {}

    void OnShutdown(Beast::error_code ec)
    {
        LOG_D("HTTP client attempting shutdown: %s.", ec.message().c_str());
        if (ec == Net::error::eof || ec == Ssl::error::stream_truncated) {
            ec = {};
        }
        if (ec) {
            LOG_E("[%s] [HttpClient] HTTP client failed during shutdown, reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
                ec.message().c_str());
            return StopConnection();
        }
        mStatus = static_cast<int32_t>(mParser.get().result_int());
        StopConnection();
    }

    Net::io_context& mIoc;
    Tcp::resolver mResolver;
    Stream &mStream;
    Net::steady_timer mTimer;
    Http::request<Http::string_body> mReq;
    Request mRawReq = {"", Http::verb::post, {}, ""};
    Http::response_parser<Http::dynamic_body> mParser;
    Beast::flat_buffer mBuffer;
    std::string mHost;
    int32_t mStatus = -1;
};

template <>
void Connection<Beast::ssl_stream<Beast::tcp_stream>>::OnHandshake(Beast::error_code ec)
{
    LOG_D("HTTP client attempting handshake: %s.", ec.message().c_str());
    if (ec) {
        LOG_E("[%s] [HttpClient] HTTP client failed during handshake, reason: %s.",
            GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return StopConnection();
    }

    // 设置HTTP GET请求
    mReq.set(Http::field::host, mHost);
    for (auto i = mRawReq.header.begin(); i != mRawReq.header.end(); ++i) {
        mReq.set(i->first, i->second);
    }
    mReq.body() = mRawReq.body;
    mReq.version(11); // 11 http1.1
    mReq.method(mRawReq.method);
    mReq.target(mRawReq.target);
    mReq.prepare_payload();

    // 发送HTTP请求到远程主机
    Http::async_write(mStream,
        mReq,
        Beast::bind_front_handler(&Connection<Beast::ssl_stream<Beast::tcp_stream>>::OnWrite,
            this->shared_from_this()));
}

template <>
void Connection<Beast::ssl_stream<Beast::tcp_stream>>::OnConnect(
    Beast::error_code ec, Tcp::resolver::results_type::endpoint_type)
{
    LOG_D("HTTP client attempting to connect: %s.", ec.message().c_str());
    if (ec) {
        return StopConnection();
    }

    // 执行SSL握手
    mStream.async_handshake(Ssl::stream_base::client, Beast::bind_front_handler(
        &Connection<Beast::ssl_stream<Beast::tcp_stream>>::OnHandshake, this->shared_from_this()));
}

template <>
void Connection<Beast::ssl_stream<Beast::tcp_stream>>::OnRead(Beast::error_code ec, std::size_t bytesTransferred)
{
    LOG_D("HTTP client attempting to read: %s.", ec.message().c_str());
    boost::ignore_unused(bytesTransferred);

    if (ec) {
        LOG_E("[%s] [HttpClient] HTTP client failed during read operation, reason: %s.",
            GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return StopConnection();
    }

    // 优雅关闭流
    mStream.async_shutdown(Beast::bind_front_handler(
        &Connection<Beast::ssl_stream<Beast::tcp_stream>>::OnShutdown, this->shared_from_this()));
}

template<>
void Connection<Beast::tcp_stream>::OnConnect(Beast::error_code ec, Tcp::resolver::results_type::endpoint_type)
{
    LOG_D("HTTP client attempting to connect: %s.", ec.message().c_str());
    if (ec) {
        LOG_W("[%s] [HttpClient] HTTP client failed to connect, reason: %s.",
            GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return StopConnection();
    }

    // 设置HTTP GET请求
    mReq.set(Http::field::host, mHost);
    for (auto i = mRawReq.header.begin(); i != mRawReq.header.end(); ++i) {
        mReq.set(i->first, i->second);
    }
    mReq.body() = mRawReq.body;
    mReq.version(11); // 11 http1.1
    mReq.method(mRawReq.method);
    mReq.target(mRawReq.target);
    mReq.prepare_payload();

    // 发送HTTP请求到远程主机
    Http::async_write(
        mStream, mReq, Beast::bind_front_handler(&Connection<Beast::tcp_stream>::OnWrite, this->shared_from_this()));
}

template <>
void Connection<Beast::tcp_stream>::OnRead(Beast::error_code ec, std::size_t bytesTransferred)
{
    LOG_D("Http client on read: %s.", ec.message().c_str());
    boost::ignore_unused(bytesTransferred);
    if (ec) {
        LOG_E("[%s] [HttpClient] HTTP client failed during read operation, reason: %s.",
            GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return StopConnection();
    }
    mStream.socket().shutdown(Tcp::socket::shutdown_both, ec);
    if (ec) {
        LOG_E("[%s] [HttpClient] HTTP client failed during socket shutdown, reason: %s.",
            GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return StopConnection();
    }
    mStatus = static_cast<int32_t>(mParser.get().result_int());
    StopConnection();
    return;
}

bool ClientDecrypt(Ssl::context &context, TlsItems &mTlsConfig, std::pair<char *, int32_t> &mPassword)
{
    if (DecryptPassword(1, mPassword, mTlsConfig)) {
        auto sslCtx = context.native_handle();
        SSL_CTX_set_default_passwd_cb_userdata(sslCtx, mPassword.first);
        return true;
    } else {
        LOG_E("[%s] [HttpClient] Failed to decrypt the client password.",
            GetErrorCode(ErrorType::CALL_ERROR, CommonFeature::HTTPCLIENT).c_str());
        return false;
    }
}

static int32_t ClientSetCipherSuites(Ssl::context &context)
{
    int32_t ret = 0;
    ret = SSL_CTX_set_ciphersuites(context.native_handle(),
        "TLS_AES_128_GCM_SHA256:"
        "TLS_AES_256_GCM_SHA384:"
        "TLS_CHACHA20_POLY1305_SHA256:"
        "TLS_AES_128_CCM_SHA256");
    return ret;
}

static int32_t UseCert(Ssl::context &context, const TlsItems &mTlsConfig)
{
    std::string caStr = "";
    uint32_t mode = mTlsConfig.checkFiles ? 0400 : 0777;        // 证书的权限要求是0400, 不校验是0777
    if (FileToBuffer(mTlsConfig.caCert, caStr, mode, mTlsConfig.checkFiles) != 0) {    // 证书的权限要求是0400
        LOG_E("[%s] [HttpClient] Failed to load CA certificate content into buffer. "
            "Please verify the file path and permissions.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    Beast::error_code ec;
    context.add_certificate_authority(Net::buffer(caStr.data(), caStr.size()), ec);
    if (ec) {
        LOG_E("[%s] [HttpClient] Failed to add certificate authority from '%s', reason: %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            mTlsConfig.caCert.c_str(), ec.message().c_str());
        return -1;
    }
    std::string certPath = mTlsConfig.tlsCert;
    bool isCertExist = false;
    if (!PathCheck(certPath, isCertExist, mode, mTlsConfig.checkFiles)) {  // 证书的权限要求是0400
        LOG_E("[%s] [HttpClient] Certificate path check failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    if (!isCertExist) {
        LOG_E("[%s] [HttpClient] Certificate file is not exist.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    context.use_certificate_chain_file(certPath, ec);
    if (ec) {
        LOG_E("[%s] [HttpClient] Http client failed on use certificate chain file, reason: %s.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return -1;
    }
    return 0;
}

static int32_t UseKey(Ssl::context &context, const TlsItems &mTlsConfig)
{
    std::string keyPath = mTlsConfig.tlsKey;
    bool isKeyExist = false;
    Beast::error_code ec;
    uint32_t mode = mTlsConfig.checkFiles ? 0400 : 0640;    // 密钥的权限要求是0400, 不校验是0640
    if (!PathCheck(keyPath, isKeyExist, mode, mTlsConfig.checkFiles)) {  // 密钥的权限要求是0400
        LOG_E("[%s] [HttpClient] Key file path check failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    if (!isKeyExist) {
        LOG_E("[%s] [HttpClient] Key file is not exist.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    context.use_private_key_file(keyPath, Ssl::context::pem, ec);
    if (ec) {
        LOG_E("[%s] [HttpClient] Failed to use the private key file for the HTTP client, reason: %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return -1;
    }
    auto sslCtx = context.native_handle();
    if (SSL_CTX_check_private_key(sslCtx) == 0) {
        LOG_E("[%s] [HttpClient] Failed to check the private key for the HTTP client.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }
    return 0;
}

int32_t HttpClient::SetTlsCtx()
{
    auto ret = ClientSetCipherSuites(mContext);
    if (ret == 0) {
        ERR_print_errors_fp(stderr);
        LOG_E("[%s] [HttpClient] Server failed to set cipher suites to TLS context.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPCLIENT).c_str());
        return -1;
    }

    STACK_OF(SSL_CIPHER) *ciphers = SSL_CTX_get_ciphers(mContext.native_handle());
    if (!ciphers) {
        perror("SSL_CTX_get_ciphersuites");
        return 1;
    }

    if (UseCert(mContext, mTlsConfig) != 0) {
        return -1;
    }

    if (mUseKMC) {
        if (!ClientDecrypt(mContext, mTlsConfig, mPassword)) {
            return -1;
        }
        if (UseKey(mContext, mTlsConfig) != 0) {
            EraseDecryptedData(mPassword);
            SSL_CTX_set_default_passwd_cb_userdata(mContext.native_handle(), nullptr);
            return -1;
        }
        EraseDecryptedData(mPassword);
    } else if (UseKey(mContext, mTlsConfig) != 0) {
        SSL_CTX_set_default_passwd_cb_userdata(mContext.native_handle(), nullptr);
        return -1;
    }
    SSL_CTX_set_default_passwd_cb_userdata(mContext.native_handle(), nullptr);
    Beast::error_code ec;
    mContext.set_verify_mode(Ssl::verify_peer | Ssl::verify_fail_if_no_peer_cert, ec);
    if (ec) {
        LOG_E("[%s] [HttpClient] Failed to set SSL verify mode for the HTTP client, reason: %s.",
            GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPCLIENT).c_str(),
            ec.message().c_str());
        return -1;
    }
    auto sslCtx = mContext.native_handle();
    verifyItems.checkSubject = false;
    verifyItems.crlFile = mTlsConfig.tlsCrl;
    verifyItems.checkFiles = mTlsConfig.checkFiles;
    SSL_CTX_set_cert_verify_callback(sslCtx, VerifyCallback, static_cast<void *>(&verifyItems));
    return 0;
}

int32_t HttpClient::SendRequestTls(const Request &request, int timeoutSeconds, int retries, std::string& responseBody,
    std::string &contentType)
{
    Endpoint endpoint = {this->mHost, this->mPort};
    for (int attempt = 0; attempt < retries + 1; ++attempt) {  // 从1开始
        LOG_D("Http client attempting to send HTTP request. Retry attempt: %d of %d, timeout: %ds.",
            attempt, retries, timeoutSeconds);
        try {
            Beast::ssl_stream<Beast::tcp_stream> stream(mIoc, mContext);
            auto connection = std::make_shared<Connection<Beast::ssl_stream<Beast::tcp_stream>>>(mIoc, stream);
            if (connection == nullptr) {
                LOG_E("[%s] [HttpClient] Http client failed to create connection.",
                    GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, CommonFeature::HTTPCLIENT).c_str());
                return -1;
            }
            auto status = connection->Run(endpoint, request, timeoutSeconds, responseBody, contentType);
            if (status == -1) {
                continue;
            } else {
                return status;
            }
        } catch (const std::exception& e) {
            LOG_E("[%s] [HttpClient] Exception caught during TLS request: %s",
                GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPCLIENT).c_str(), e.what());
        }
    }
    return -1;
}

int32_t HttpClient::SendRequestNonTls(const Request &request,
    int timeoutSeconds, int retries, std::string& responseBody, std::string &contentType)
{
    Endpoint endpoint = {this->mHost, this->mPort};
    for (int attempt = 0; attempt < retries + 1; ++attempt) {
        LOG_D("Http client attempt to send request, retry time (%d/%d), timeout %ds",
            attempt, retries, timeoutSeconds);
        try {
            Beast::tcp_stream stream(mIoc);
            auto connection = std::make_shared<Connection<Beast::tcp_stream>>(mIoc, stream);
            if (connection == nullptr) {
                LOG_E("[%s] [HttpClient] Http client failed to create connection.",
                    GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, CommonFeature::HTTPCLIENT).c_str());
                return -1;
            }
            auto status = connection->Run(endpoint, request, timeoutSeconds, responseBody, contentType);
            if (status == -1) {
                continue;
            } else {
                return status;
            }
        } catch (const std::exception& e) {
            LOG_E("[%s] [HttpClient] Exception caught during non-TLS request: %s",
                GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPCLIENT).c_str(), e.what());
        }
    }
    // 如果所有重试均失败，返回最后的错误码
    return -1;
}

}  // namespace MS
}  // namespace MINDIE
