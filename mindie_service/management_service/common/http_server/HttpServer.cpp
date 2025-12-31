/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
 * MindIE is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *         http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */
#include "HttpServer.h"

#include <iostream>
#include <fstream>
#include <utility>
#include <cstring>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include "Util.h"

namespace MINDIE::MS {

static Http::message_generator ResourceNotFound(const Http::request<Http::string_body> &req, const std::string &ip,
    Beast::string_view target)
{
    LOG_M("[Handle] Handle request, IP %s, method %s, target %s, code %d.",
        ip.c_str(), std::string(req.method_string()).c_str(),
        std::string(req.target()).c_str(), static_cast<int>(Http::status::not_found));
    Http::response<Http::string_body> response{Http::status::not_found, req.version()};
    response.body() = "Cannot find the resource " + std::string(target);
    std::string contentType = "text/html";
    response.set(Http::field::content_type, contentType);
    response.keep_alive(req.keep_alive());
    response.prepare_payload();
    return response;
}

static Http::message_generator BadRequest(const Http::request<Http::string_body> &req, const std::string &ip,
    Beast::string_view reason)
{
    LOG_M("[Handle] Handle request, IP %s, method %s, target %s, code %d",
        ip.c_str(), std::string(req.method_string()).c_str(),
        std::string(req.target()).c_str(), static_cast<int>(Http::status::bad_request));
    Http::response<Http::string_body> response{Http::status::bad_request, req.version()};
    std::string contentType = "text/html";
    response.set(Http::field::content_type, contentType);
    response.body() = std::string(reason);
    response.set(Http::field::server, BOOST_BEAST_VERSION_STRING);
    response.keep_alive(req.keep_alive());
    response.prepare_payload();
    return response;
}

static Http::message_generator ResponseOK(const Http::request<Http::string_body> &req, const std::string &ip,
    Beast::string_view body)
{
    LOG_M("[Handle] Handle request, IP %s, method %s, target %s, code %d",
        ip.c_str(), std::string(req.method_string()).c_str(),
        std::string(req.target()).c_str(), static_cast<int>(Http::status::ok));
    Http::response<Http::string_body> response{Http::status::ok, req.version()};
    response.body() = std::string(body);
    std::string contentType = "application/json";
    response.set(Http::field::content_type, contentType);
    response.keep_alive(req.keep_alive());
    response.prepare_payload();
    return response;
}

static Http::message_generator ResponseError(const Http::request<Http::string_body> &req, const std::string &ip,
    Beast::string_view body)
{
    LOG_M("[Handle] Handle request, IP %s, method %s, target %s, code %d",
        ip.c_str(), std::string(req.method_string()).c_str(),
        std::string(req.target()).c_str(), static_cast<int>(Http::status::internal_server_error));
    Http::response<Http::string_body> response{Http::status::internal_server_error, req.version()};
    response.body() = std::string(body);
    std::string contentType = "application/json";
    response.set(Http::field::content_type, contentType);
    response.keep_alive(req.keep_alive());
    response.prepare_payload();
    return response;
}

static std::string GenerateResponse(const std::string &status, const Response &resp)
{
    nlohmann::json jsonObj;
    if (resp.data.size() != 0 && CheckJsonStringSize(resp.data)) {
        if (nlohmann::json::accept(resp.data)) {
            nlohmann::json dataJsonObj = nlohmann::json::parse(resp.data, CheckJsonDepthCallBack);
            jsonObj["data"] = dataJsonObj;
        } else {
            jsonObj["data"] = resp.data;
        }
    }
    jsonObj["message"] = resp.message;
    jsonObj["status"] = status;
    return jsonObj.dump();
}

std::map<std::string, UrlCallBack>& HttpServer::GetUrlHandler(Http::request<Http::string_body> &req)
{
    if (req.method() == Http::verb::post) {
        return mPostUrlHandler;
    } else if (req.method() == Http::verb::get) {
        return mGetUrlHandler;
    } else {
        return mDeleteUrlHandler;
    }
}

Http::message_generator HttpServer::HandleRequest(Http::request<Http::string_body> &&req, const std::string &ip)
{
    if (req.method() != Http::verb::get && req.method() != Http::verb::post &&
        req.method() != Http::verb::delete_) {
        return BadRequest(req, ip, "Unknown Http-method");
    }

    if (req.target().empty() || req.target()[0] != '/' || req.target().find("..") != Beast::string_view::npos) {
        return BadRequest(req, ip, "Illegal request-target");
    }
    req.set("X-Real-IP", ip);
    std::map<std::string, UrlCallBack> &handler = GetUrlHandler(req);
    for (auto iter : std::as_const(handler)) {
        if (!req.target().starts_with(iter.first)) {
            continue;
        }
        auto ret = iter.second(req);
        if (ret.first == ErrorCode::OK) {
            return ResponseOK(req, ip, GenerateResponse("0", ret.second));
        } else if (ret.first == ErrorCode::NOT_FOUND) {
            return ResourceNotFound(req, ip, req.target());
        }
        return ResponseError(req, ip, GenerateResponse("1", ret.second));
    }
    return ResourceNotFound(req, ip, req.target());
}

class Connection : public std::enable_shared_from_this<Connection> {
public:
    explicit Connection(Net::io_context &ioc, Tcp::socket &&socket, HttpServer &httpServer)
        : mStream(std::move(socket)), mTimer(ioc), mHttpServer(httpServer)
    {}
    Connection(const Connection &) = delete;
    Connection& operator=(const Connection &) = delete;
    ~Connection()
    {
        mHttpServer.SubConnection();
    }
    void Run(uint32_t timeoutSeconds)
    {
        SetTimeout(timeoutSeconds);
        mHttpServer.AddConnection();
        mParser.body_limit(bodyLimit);
        mParser.header_limit(headLimit);

        Http::async_read(
            mStream, mBuffer, mParser, Beast::bind_front_handler(&Connection::OnRead, this->shared_from_this()));
    }

private:
    void Stop()
    {
        mTimer.cancel();
        Beast::get_lowest_layer(mStream).cancel();
        LOG_I("HTTP server Connection stoped.");
    }

    void SetTimeout(uint32_t timeoutSeconds)
    {
        mTimer.expires_after(std::chrono::seconds(timeoutSeconds));
        mTimer.async_wait(Beast::bind_front_handler(&Connection::OnTimeout, this->shared_from_this()));
    }

    void OnTimeout(Beast::error_code ec)
    {
        if (ec != Net::error::operation_aborted) {
            LOG_E("[%s] [HttpServer] HTTP server on timeout.",
                GetErrorCode(ErrorType::DEADLINE_EXCEEDED, CommonFeature::HTTPSERVER).c_str());
            Stop();
        }
    }

    void OnRead(Beast::error_code ec, std::size_t bytesTransferred)
    {
        LOG_D("HTTP server on read: %s.", ec.message().c_str());
        boost::ignore_unused(bytesTransferred);

        if (ec == Http::error::end_of_stream) {
            mStream.socket().shutdown(Tcp::socket::shutdown_send, ec);
            return;
        }

        if (ec) {
            LOG_E("[%s] [HttpServer] HTTP server failed on read: %s.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            Stop();
            return;
        }
        try {
            std::string ip = mStream.socket().remote_endpoint().address().to_string();
            // Send the response
            SendResponse(this->mHttpServer.HandleRequest(std::move(mParser.get()), ip));
        } catch (const std::exception& e) {
            Stop();
            LOG_E("[%s] [HttpServer] On read failed, error is %s.",
                  GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPSERVER).c_str(), e.what());
        }
    }

    void SendResponse(Http::message_generator &&msg)
    {
        // Write the response
        Beast::async_write(
            mStream, std::move(msg), Beast::bind_front_handler(&Connection::OnWrite, this->shared_from_this()));
    }

    void OnWrite(Beast::error_code ec, std::size_t bytesTransferred)
    {
        LOG_D("HTTP server on write, error is %s.", ec.message().c_str());
        boost::ignore_unused(bytesTransferred);

        if (ec) {
            LOG_E("[%s] [HttpServer] HTTP server failed on write, error is %s.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            // 这里是不是要加处理，发送失败了断开会话
            Stop();
            return;
        }
        mStream.socket().shutdown(Tcp::socket::shutdown_send, ec);
        mTimer.cancel();
    }

    Beast::tcp_stream mStream;
    Beast::flat_buffer mBuffer;
    Http::request_parser<Http::string_body> mParser;
    Net::steady_timer mTimer;
    uint32_t bodyLimit = 10 * 1024 * 1024;
    uint32_t headLimit = 8 * 1024;
    HttpServer &mHttpServer;
};

class ConnectionTls : public std::enable_shared_from_this<ConnectionTls> {
public:
    explicit ConnectionTls(Net::io_context &ioc, Tcp::socket &&socket, Ssl::context &mSslCtx,
        HttpServer &httpServer)
        : mStream(std::move(socket), mSslCtx), mTimer(ioc), mHttpServer(httpServer)
    {}
    ConnectionTls(const ConnectionTls &) = delete;
    ConnectionTls& operator=(const ConnectionTls &) = delete;
    ~ConnectionTls()
    {
        mHttpServer.SubConnection();
    }
    void Run(uint32_t timeoutSeconds)
    {
        SetTimeout(timeoutSeconds);
        mHttpServer.AddConnection();
        mStream.async_handshake(
            Ssl::stream_base::server, Beast::bind_front_handler(&ConnectionTls::OnHandshake, this->shared_from_this()));
    }
    void Stop()
    {
        mTimer.cancel();
        Beast::get_lowest_layer(mStream).cancel();
        LOG_I("HTTP server connection stoped.");
    }

private:
    void SetTimeout(uint32_t timeoutSeconds)
    {
        mTimer.expires_after(std::chrono::seconds(timeoutSeconds));
        mTimer.async_wait(Beast::bind_front_handler(&ConnectionTls::OnTimeout, this->shared_from_this()));
    }

    void OnTimeout(Beast::error_code ec)
    {
        if (ec != Net::error::operation_aborted) {
            LOG_E("[%s] [HttpServer] HTTP server on timeout.",
                GetErrorCode(ErrorType::DEADLINE_EXCEEDED, CommonFeature::HTTPSERVER).c_str());
            Stop();
        }
    }

    void OnHandshake(Beast::error_code ec)
    {
        LOG_D("HTTP server on handshake, error is %s.", ec.message().c_str());
        if (ec) {
            Stop();
            LOG_E("[%s] [HttpServer] HTTP server failed on handshake, error is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            return;
        }
        mParser.body_limit(bodyLimit);
        mParser.header_limit(headLimit);

        Http::async_read(
            mStream, mBuffer, mParser, Beast::bind_front_handler(&ConnectionTls::OnRead, this->shared_from_this()));
    }

    void OnRead(Beast::error_code ec, std::size_t bytesTransferred)
    {
        LOG_D("HTTP server on read, error is %s.", ec.message().c_str());
        boost::ignore_unused(bytesTransferred);

        if (ec == Http::error::end_of_stream) {
            mStream.async_shutdown(Beast::bind_front_handler(&ConnectionTls::OnShutdown, this->shared_from_this()));
            return;
        }

        if (ec) {
            LOG_E("[%s] [HttpServer] HTTP server failed on read, error is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            Stop();
            return;
        }
        try {
            std::string ip = Beast::get_lowest_layer(mStream).socket().remote_endpoint().address().to_string();
            // Send the response
            SendResponse(this->mHttpServer.HandleRequest(std::move(mParser.get()), ip));
        } catch (const std::exception& e) {
            LOG_E("[%s] [HttpServer] HTTP server failed on read, error is %s.",
                  GetErrorCode(ErrorType::EXCEPTION, CommonFeature::HTTPSERVER).c_str(), e.what());
        }
    }

    void SendResponse(Http::message_generator &&msg)
    {
        // Write the response
        Beast::async_write(
            mStream, std::move(msg), Beast::bind_front_handler(&ConnectionTls::OnWrite, this->shared_from_this()));
    }

    void OnWrite(Beast::error_code ec, std::size_t bytesTransferred)
    {
        LOG_D("HTTP server on write, error is %s.", ec.message().c_str());
        boost::ignore_unused(bytesTransferred);

        if (ec) {
            LOG_E("[%s] [HttpServer] HTTP server failed on write, error is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            Stop();
            return;
        }

        mStream.async_shutdown(Beast::bind_front_handler(&ConnectionTls::OnShutdown,
            this->shared_from_this()));
    }

    void OnShutdown(Beast::error_code ec)
    {
        LOG_D("HTTP server on shutdown, error is %s.", ec.message().c_str());
        if (ec) {
            LOG_E("[%s] [HttpServer] HTTP server on shutdown, error is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
        }
        Stop();
    }

    Beast::ssl_stream<Beast::tcp_stream> mStream;
    Beast::flat_buffer mBuffer;
    Http::request_parser<Http::string_body> mParser;
    Net::steady_timer mTimer;
    uint32_t bodyLimit = 10 * 1024 * 1024;
    uint32_t headLimit = 8 * 1024;
    HttpServer &mHttpServer;
};

class Listener : public std::enable_shared_from_this<Listener> {
public:
    explicit Listener(Net::io_context &ioc, Ssl::context &sslCtx, HttpServer &httpServer)
        : mIoc(ioc), mSignals(ioc, SIGINT, SIGTERM), mAcceptor(ioc), mSslCtx(sslCtx), mHttpServer(httpServer) {}

    int32_t Run(const boost::asio::ip::address &address, uint32_t port, bool useTls, uint32_t timeOut)
    {
        Beast::error_code ec;
        auto endpoint = Tcp::endpoint(address, port);
        mAcceptor.open(endpoint.protocol(), ec);
        if (ec) {
            LOG_E("[%s] [HttpServer] Listener failed on open the endpoint, reason is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            return -1;
        }

        // Allow address reuse
        mAcceptor.set_option(Net::socket_base::reuse_address(true), ec);
        if (ec) {
            LOG_E("[%s] [HttpServer] Listener failed on set reuse adddress, reason is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            return -1;
        }

        // Bind to the server address
        mAcceptor.bind(endpoint, ec);
        if (ec) {
            LOG_E("[%s] [HttpServer] Listener failed on bind endpoint, reason is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            return-1;
        }

        // Start listening for connections
        mAcceptor.listen(Net::socket_base::max_listen_connections, ec);
        if (ec) {
            LOG_E("[%s] [HttpServer] Listener failed on listen, reason is %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            return-1;
        }
        mSignals.async_wait([this](__attribute__((unused)) Beast::error_code ec, int signal) {
            LOG_M("[Stop] Received signal %d, stop the HTTP server.", signal);
            mAcceptor.close();
            mIoc.stop();
        });
        mUseTls = useTls;
        mTimeout = timeOut;
        this->mAcceptor.async_accept(Net::make_strand(mIoc), Beast::bind_front_handler(
            &Listener::OnAccept, this->shared_from_this()));
        return 0;
    }

private:
    void OnAccept(Beast::error_code ec, Tcp::socket socket)
    {
        LOG_D("Listener on accept.");
        if (ec) {
            LOG_E("[%s] [HttpServer] Listener failed on accept, reason is %s.",
                GetErrorCode(ErrorType::RESOURCE_LIMIT, CommonFeature::HTTPSERVER).c_str(),
                ec.message().c_str());
            return;
        }

        if (mHttpServer.ReachMaxConnection()) {
            LOG_E("[%s] [HttpServer] Listener failed on accept, because connection is full.",
                GetErrorCode(ErrorType::RESOURCE_LIMIT, CommonFeature::HTTPSERVER).c_str());
            socket.close();
        } else {
            if (mUseTls) {
                auto connection = std::make_shared<ConnectionTls>(mIoc, std::move(socket), mSslCtx, mHttpServer);
                if (connection == nullptr) {
                    LOG_E("[%s] [HttpServer] Failed to create TLS connection.",
                        GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, CommonFeature::HTTPSERVER).c_str());
                    return;
                }
                connection->Run(mTimeout);
            } else {
                auto connection = std::make_shared<Connection>(mIoc, std::move(socket), mHttpServer);
                if (connection == nullptr) {
                    LOG_E("[%s] [HttpServer] Failed to create connection.",
                        GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, CommonFeature::HTTPSERVER).c_str());
                    return;
                }
                connection->Run(mTimeout);
            }
        }

        this->mAcceptor.async_accept(
            Net::make_strand(mIoc), Beast::bind_front_handler(&Listener::OnAccept, this->shared_from_this()));
    }
    Net::io_context &mIoc;
    Net::signal_set mSignals;
    bool mUseTls = true;
    Tcp::acceptor mAcceptor;
    uint32_t mTimeout = 10; // 10 一次请求处理的最大时间
    Ssl::context &mSslCtx;
    HttpServer &mHttpServer;
};

bool ServerDecrypt(Ssl::context &mSslCtx, const MINDIE::MS::HttpServerParams &httpParams,
    std::pair<char *, int32_t> &mPassword)
{
    if (DecryptPassword(1, mPassword, httpParams.serverTlsItems)) {
        auto sslCtx = mSslCtx.native_handle();
        SSL_CTX_set_default_passwd_cb_userdata(sslCtx, mPassword.first);
        return true;
    } else {
        LOG_E("[%s] [HttpServer] Decrypt server password failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPSERVER).c_str());
        return false;
    }
}

static int32_t SetCipherSuites(Ssl::context &mSslCtx)
{
    int32_t ret = 0;
    ret = SSL_CTX_set_ciphersuites(mSslCtx.native_handle(),
        "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_128_CCM_SHA256");
    return ret;
}

static int32_t UseCertificate(Ssl::context &mSslCtx, const MINDIE::MS::HttpServerParams &httpParams)
{
    std::string caStr = "";
    uint32_t mode = httpParams.serverTlsItems.checkFiles ? 0400 : 0777;     // 证书的权限要求是0400, 不校验是0777
    if (FileToBuffer(httpParams.serverTlsItems.caCert, caStr, mode, httpParams.serverTlsItems.checkFiles) != 0) {
        LOG_E("[%s] [HttpServer]  Failed to convert CA file %s to buffer.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPSERVER).c_str(),
            httpParams.serverTlsItems.caCert.c_str());
        return -1;
    }
    boost::system::error_code ec;
    mSslCtx.add_certificate_authority(Net::buffer(caStr.data(), caStr.size()), ec);
    if (ec) {
        LOG_E("[%s] [HttpServer] Failed to add certificate authority from file %s. Reason is %s.",
              GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPSERVER).c_str(),
              httpParams.serverTlsItems.caCert.c_str(), ec.message().c_str());
        return -1;
    }
    std::string certPath = httpParams.serverTlsItems.tlsCert;
    bool isFileExist = false;
    if (!PathCheck(certPath, isFileExist, mode, httpParams.serverTlsItems.checkFiles)) {  // 证书的权限要求是0400
        LOG_E("[%s] [HttpServer] Certificate path check failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPSERVER).c_str());
        return -1;
    }
    if (!isFileExist) {
        LOG_E("[%s] [HttpServer] Certificate file is not exist.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPSERVER).c_str());
        return -1;
    }
    mSslCtx.use_certificate_chain_file(certPath, ec);
    if (ec) {
        LOG_E("[%s] [HttpServer] HTTP server failed on use certificate chain file, reason is %s.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPSERVER).c_str(),
            ec.message().c_str());
        return -1;
    }
    return 0;
}

static int32_t UsePrivateKey(Ssl::context &mSslCtx, const MINDIE::MS::HttpServerParams &httpParams)
{
    boost::system::error_code ec;
    std::string keyPath = httpParams.serverTlsItems.tlsKey;
    bool isFileExist = false;
    uint32_t mode = httpParams.serverTlsItems.checkFiles ? 0400 : 0640; // 密钥的权限要求是0400, 不校验是0640
    if (!PathCheck(keyPath, isFileExist, mode, httpParams.serverTlsItems.checkFiles)) {   // 密钥的权限要求是0400
        LOG_E("[%s] [HttpServer] Key file path check failed.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPSERVER).c_str());
        return -1;
    }
    if (!isFileExist) {
        LOG_E("[%s] [HttpServer] Key file is not exist.",
            GetErrorCode(ErrorType::NOT_FOUND, CommonFeature::HTTPSERVER).c_str());
        return -1;
    }
    mSslCtx.use_private_key_file(keyPath, Ssl::context::pem, ec);
    if (ec) {
        LOG_E("[%s] [HttpServer] HTTP server failed on use private key file, reason is %s.",
            GetErrorCode(ErrorType::INVALID_INPUT, CommonFeature::HTTPSERVER).c_str(),
            ec.message().c_str());
        return -1;
    }
    return 0;
}

static void SetVerifyCallback(Ssl::context &mSslCtx, const MINDIE::MS::HttpServerParams &httpParams,
    VerifyItems &verifyItems)
{
    verifyItems.checkSubject = httpParams.checkSubject;
    verifyItems.crlFile = httpParams.serverTlsItems.tlsCrl;
    verifyItems.organization = "msgroup";
    verifyItems.commonName = "msclientuser";
    verifyItems.interCACommonName = "mindiems";
    verifyItems.checkFiles = httpParams.serverTlsItems.checkFiles;
    SSL_CTX_set_cert_verify_callback(mSslCtx.native_handle(), VerifyCallback, static_cast<void *>(&verifyItems));
}

int HttpServer::Run(const MINDIE::MS::HttpServerParams &httpParams)
{
    auto const address = Net::ip::make_address(httpParams.ip);
    if (httpParams.serverTlsItems.tlsEnable) {
        if (SetCipherSuites(mSslCtx) == 0) {
            LOG_E("[%s] [HttpServer] Server Failed to set cipher suites to TLS context.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPSERVER).c_str());
            return -1;
        }

        if (UseCertificate(mSslCtx, httpParams) != 0) {
            return -1;
        }

        if (!ServerDecrypt(mSslCtx, httpParams, mPassword)) {
            return -1;
        }

        LOG_M("[Load] Load TLS private key file %s", httpParams.serverTlsItems.tlsKey.c_str());
        std::cout << "Loading TLS private key file " << FilterLogStr(httpParams.serverTlsItems.tlsKey) << std::endl;

        if (UsePrivateKey(mSslCtx, httpParams) != 0) {
            SSL_CTX_set_default_passwd_cb_userdata(mSslCtx.native_handle(), nullptr);
            EraseDecryptedData(mPassword);
            return -1;
        }
        SSL_CTX_set_default_passwd_cb_userdata(mSslCtx.native_handle(), nullptr);
        EraseDecryptedData(mPassword);
        boost::system::error_code ec;
        mSslCtx.set_verify_mode(Ssl::verify_peer | Ssl::verify_fail_if_no_peer_cert, ec);
        if (ec) {
            LOG_E("[%s] [HttpServer] HTTP server failed on set verify mode, reason is %s.",
                GetErrorCode(ErrorType::INVALID_PARAMETER, CommonFeature::HTTPSERVER).c_str(), ec.message().c_str());
            return -1;
        }
        SetVerifyCallback(mSslCtx, httpParams, verifyItems);
    }
    auto listener = std::make_shared<Listener>(mIoc, mSslCtx, *this);
    if (listener == nullptr) {
        LOG_E("[%s] [HttpServer] Failed to create listener.",
            GetErrorCode(ErrorType::RESOURCE_EXHAUSTED, CommonFeature::HTTPSERVER).c_str());
        return -1;
    }
    if (listener->Run(address, httpParams.port, httpParams.serverTlsItems.tlsEnable, mTimeOut) != 0) {
        LOG_E("[%s] [HttpServer] Failed to run listener.",
            GetErrorCode(ErrorType::UNREACHABLE, CommonFeature::HTTPSERVER).c_str());
        return -1;
    }
    LOG_M("[Start] Succeed to start the HTTP server!");
    std::cout << "succeed to start the HTTP server!" << std::endl;
    mIoc.run();
    return 0;
}

int HttpServer::Stop()
{
    this->mIoc.stop();
    LOG_M("[Stop] Succeed to stop the HTTP server!");
    return 0;
}

}  // namespace MINDIE::MS