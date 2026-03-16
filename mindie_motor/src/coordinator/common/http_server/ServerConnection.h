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
#ifndef MINDIE_MS_COORDINATOR_SERVER_CONNECTION_H
#define MINDIE_MS_COORDINATOR_SERVER_CONNECTION_H

#include <utility>
#include <memory>
#include "boost/asio.hpp"
#include "boost/asio/ssl/stream.hpp"
#include "boost/asio/steady_timer.hpp"
#include "boost/asio/ssl/error.hpp"
#include "boost/beast/core.hpp"
#include "boost/beast/http.hpp"
#include "boost/beast/version.hpp"
#include "boost/beast/ssl.hpp"
#include "ServerHandler.h"
#include "WriteDeque.h"
#include "msServiceProfiler/Tracer.h"

#include "Logger.h"

namespace MINDIE::MS {

using TCPStream = boost::beast::tcp_stream;
using SSLStream = boost::beast::ssl_stream<boost::beast::tcp_stream>;
using SSLContext = boost::asio::ssl::context;

class HttpServer;

struct ServerRes {
    bool isFinish = true;
    boost::beast::http::status state = boost::beast::http::status::ok;
    std::string id = "";
    std::string body = "";
    std::string contentType = "";
};

/// Connection of http server.
///
/// Connection of http server.
class ServerConnection : public virtual std::enable_shared_from_this<ServerConnection> {
public:
    explicit ServerConnection(boost::asio::io_context& ioContext, ServerHandler& serverHandler, HttpServer &server);
    ~ServerConnection() = default;
    void Start(size_t timeout, size_t maxKeepAliveReqs, size_t keepAliveSInit);
    TCPStream& GetSocket();
    virtual SSLStream* GetSSLSocket()
    {
        return nullptr;
    }
    boost::beast::http::request<boost::beast::http::dynamic_body>& GetReq();

    /// Send http response.
    ///
    /// Send response from http server to http client.
    ///
    /// \param res http response.
    void SendRes(const ServerRes &res);

    // 增加一些维测性
    void SetConnectionID(uint32_t connectionID) { mConnectionID = connectionID;}
    uint32_t GetConnectionId() const
    {
        return mConnectionID;
    }
    void SetReqId(const std::string &reqId)
    {
        mReqId = reqId;
    }
    std::string GetReqId() const
    {
        return mReqId;
    }
    virtual void DoClose(boost::beast::error_code ec);

protected:
    ServerConnection(const ServerConnection& other) = delete;
    ServerConnection& operator=(const ServerConnection& other) = delete;

    virtual void DoRead();
    /// Read http request handler.
    ///
    /// When a read event is triggered, this function will be called.
    ///
    /// \param ec error code.
    /// \param bytes read bytes.
    void OnRead(boost::beast::error_code ec, std::size_t bytes);

    // 处理非chunk类单条数据
    virtual void DoWriteFinishRes(const ServerRes &res);
    void OnWriteFinishRes(bool keepAlive, boost::beast::error_code ec, size_t bytes);

    // 处理chunk类数据
    void DoWriteChunk();
    // 同步接口，写chunk头
    virtual void WriteChunkHeader(const ServerRes &res);
    // 异步接口，写chunk 内容
    virtual void WriteChunk(const ServerRes &res);
    void OnWriteChunk(bool isFinish, boost::beast::error_code ec, size_t bytes);
    // 异步接口，写chunk尾巴
    virtual void WriteChunkTailer();
    void OnWriteChunkTailer(bool keepAlive, boost::beast::error_code ec, size_t bytes);

    boost::asio::const_buffer GetChunk(boost::beast::string_view s) const;

    bool keepAlive_ = false;
    unsigned version_ = 11; // 11表示http1.1
    HttpServer &server_;
    size_t timeout_ = 0;
    size_t keepAliveS = 0;
    size_t maxKeepAliveReqs_ = 1; // 用户使用同一链接发送请求的最大数量，达到上限后链接关闭，防止用户一直占用链接
    std::atomic<bool> isClose;
    std::atomic<bool> chunking;

    std::atomic<bool> asyncReadPending = false;
    std::atomic<bool> asyncChunkPending = false;
    std::atomic<bool> asyncNotChunkPending = false;

    std::atomic<size_t> reqNum;
    TCPStream stream_;
    boost::beast::flat_buffer buffer_;
    boost::optional<boost::beast::http::request_parser<boost::beast::http::dynamic_body>> req_;
    boost::beast::http::response<boost::beast::http::dynamic_body> res_;
    ServerHandler& serverHandler_;
    boost::asio::steady_timer mTimer;
    WriteDeque<ServerRes> resQueue;
    uint32_t mConnectionID = 0;
    std::mutex resMtx;
    std::string mReqId;
    static constexpr uint32_t HEADER_LIMIT = 8 * 1024; // 请求头的最大上限8 * 1024

    // Trace 采样
    std::unique_ptr<msServiceProfiler::Span> span = nullptr;
    size_t attachIndex = 0;

private:
    void StartRequestTrace(boost::beast::http::request<boost::beast::http::dynamic_body>& req);
    void SetAttributesTrace(boost::beast::http::request<boost::beast::http::dynamic_body>& req);
    void SetResponseStatusTrace(const ServerRes& res);
    void EndRequestTrace();
};

}
#endif