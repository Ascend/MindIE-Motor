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
#include "Logger.h"
#include "HttpServer.h"
#include "Configure.h"
#include "ServerConnection.h"

namespace MINDIE::MS {
ServerConnection::ServerConnection(boost::asio::io_context& ioContext, ServerHandler& serverHandler,
    HttpServer &server) : server_(server), isClose(false), chunking(false), reqNum(0),
    stream_(boost::asio::make_strand(ioContext)), serverHandler_(serverHandler), mTimer(ioContext) {}

void ServerConnection::Start(size_t timeout, size_t maxKeepAliveReqs, size_t keepAliveSInit)
{
    timeout_ = timeout;
    maxKeepAliveReqs_ = maxKeepAliveReqs;
    keepAliveS = keepAliveSInit;
    DoRead();
}

TCPStream& ServerConnection::GetSocket()
{
    return stream_;
}

// 针对业务上如果没有做好拦截，导致DoWriteFinishRes 和 DoWriteChunk同时来的场景。
void ServerConnection::SendRes(const ServerRes &res)
{
    std::lock_guard<std::mutex> lck(resMtx);
    if (isClose) {
        LOG_E("[%s] [ServerConnection] Server connection is close,  send request failed, id is %s.",
            GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::SERVER_CONNECTION).c_str(),
            mReqId.c_str());
        return;
    }

    if (asyncReadPending) {
        LOG_E("[%s] [ServerConnection] Asynchronous read pending, refuse to send request, connection id is %u, "
            "request id is %s.", GetErrorCode(ErrorType::PENDING, CoordinatorFeature::SERVER_CONNECTION).c_str(),
            mConnectionID, mReqId.c_str());
        return;
    }

    // 外部使用者需要保证 1个 请求的 DoWriteFinishRes， 只能有一次。
    if (res.isFinish && !chunking) {    // chunking 表征，所有chunk未回复完毕
        if (asyncChunkPending) {  // 避免正在发的时候，chunk那条路有请求
            LOG_E("[%s] [ServerConnection] Asynchronous chunk pending, refuse to finish request, connection id is %u, "
                "request id is %s.", GetErrorCode(ErrorType::PENDING, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                mConnectionID, mReqId.c_str());
            return;
        }

        // 避免业务侧发两次finish
        if (asyncNotChunkPending) {
            LOG_E("[%s] [ServerConnection] Asynchronous not chunk pending, refuse to finish request, "
                "connection id is %u, request id is %s.",
                GetErrorCode(ErrorType::PENDING, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                mConnectionID, mReqId.c_str());
            return;
        }
        // 一条消息，直接发完，应答完全的场景（可能是D的最后一个token，也可能是P的首token即结束），走这条路
        DoWriteFinishRes(res);
    } else {
        // 避免chunk发的时候，not chunk那条路有请求
        if (asyncNotChunkPending) {
            LOG_E("[%s] [ServerConnection] Asynchronous not chunk pending, refuse to finish request, "
                "connection id is %u, request id is %s.",
                GetErrorCode(ErrorType::PENDING, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                mConnectionID, mReqId.c_str());
            return;
        }
        // chunk类的消息，用queue转发
        resQueue.PushBack(res);
        LOG_D("[ServerConnection] Push back res to queue, request ID is %s, resQueue.size() is %zu",
            mReqId.c_str(), resQueue.Size());
        // asyncChunkPending 表征，正在发送某个chunk的执行过程中
        if (!asyncChunkPending) {
            DoWriteChunk();
        }
    }
}

void ServerConnection::DoRead()
{
    std::lock_guard<std::mutex> lck(resMtx);
    asyncReadPending = true;
    auto self(shared_from_this());
    req_.emplace();
    req_->body_limit(Configure::Singleton()->reqLimit.bodyLimit);
    req_->header_limit(ServerConnection::HEADER_LIMIT);
    buffer_.clear();
    if (keepAliveS > 0) {
        stream_.expires_after(std::chrono::seconds(keepAliveS));
    } else {
        size_t timeoutOneYear = 31622400;
        stream_.expires_at(std::chrono::steady_clock::now() + std::chrono::seconds(timeoutOneYear));
    }
    boost::beast::http::async_read(stream_, buffer_, *req_, boost::beast::bind_front_handler(&ServerConnection::OnRead,
        self));
}

void ServerConnection::OnRead(boost::beast::error_code ec, std::size_t bytes)
{
    asyncReadPending = false;
    boost::ignore_unused(bytes);
    if (ec) {
        if (ec != boost::beast::http::error::end_of_stream) {
            LOG_E("[%s] [ServerConnection] Read operation failed. Reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                ec.what().c_str());
            auto fun = serverHandler_.GetFun(ServerHandlerType::REQ_ERROR);
            if (fun != nullptr) {
                fun(shared_from_this());
            }
        }
        return DoClose(ec);
    }
    auto &req = req_->get();
    reqNum++;
    keepAlive_ = req.keep_alive();
    if (reqNum > maxKeepAliveReqs_) {
        keepAlive_ = false;
        ServerRes serverRes;
        serverRes.body = "Connection request out of limit, max is " + std::to_string(maxKeepAliveReqs_) + "\r\n";
        serverRes.contentType = "text/plain";
        serverRes.state = boost::beast::http::status::too_many_requests;
        DoWriteFinishRes(serverRes);
        return;
    }
    version_ = req.version();
    auto fun = serverHandler_.GetFun(req.method(), req.target());
    if (fun == nullptr) {
        keepAlive_ = false;
        ServerRes serverRes;
        serverRes.body = "Invalid request\r\n";
        serverRes.contentType = "text/plain";
        serverRes.state = boost::beast::http::status::bad_request;
        DoWriteFinishRes(serverRes);
    } else {
        // 原地写回结果的场景，补充考虑。
        fun(shared_from_this()); // 外部记得保存this的指针，防止引用计数到零后释放
    }
}

boost::beast::http::request<boost::beast::http::dynamic_body>& ServerConnection::GetReq()
{
    return req_->get();
}

void ServerConnection::DoWriteFinishRes(const ServerRes &res)
{
    asyncNotChunkPending = true;
    auto self(shared_from_this());
    res_ = {};
    res_.set(boost::beast::http::field::server, Configure::Singleton()->httpConfig.serverName);
    res_.set(boost::beast::http::field::content_type, res.contentType + "; charset=utf-8");
    res_.version(version_);
    res_.keep_alive(keepAlive_);
    res_.result(res.state);
    boost::beast::ostream(res_.body()) << res.body;
    res_.prepare_payload();
    if (timeout_ > 0) {
        stream_.expires_after(std::chrono::seconds(timeout_));
    } else {
        size_t timeoutOneYear = 31622400;
        stream_.expires_at(std::chrono::steady_clock::now() + std::chrono::seconds(timeoutOneYear));
    }
    boost::beast::http::async_write(stream_, res_,
        boost::beast::bind_front_handler(&ServerConnection::OnWriteFinishRes, self, keepAlive_));
}

void ServerConnection::OnWriteFinishRes(bool keepAlive, boost::beast::error_code ec, size_t bytes)
{
    boost::ignore_unused(bytes);
    if (ec) {
        if (ec != boost::beast::http::error::end_of_stream) {
            LOG_E("[%s] [ServerConnection] Write operation failed. Reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                ec.what().c_str());
            auto fun = serverHandler_.GetFun(ServerHandlerType::RES_ERROR);
            if (fun != nullptr) {
                fun(shared_from_this());
            }
        }
        return DoClose(ec);
    }

    auto fun = serverHandler_.GetFun(ServerHandlerType::RES);
    if (fun != nullptr) {
        fun(shared_from_this());
    }

    if (!keepAlive) {
        return DoClose(ec);
    }

    asyncNotChunkPending = false;
    // 一个非chunk消息完成，开始下一次Read, 此时如果有chunk消息来临，则会异常
    DoRead();
}

void ServerConnection::WriteChunkHeader(const ServerRes &res)
{
    boost::beast::http::response<boost::beast::http::empty_body> headerRes{
        boost::beast::http::status::ok, version_};
    headerRes.set(boost::beast::http::field::server, Configure::Singleton()->httpConfig.serverName);
    headerRes.set(boost::beast::http::field::content_type, res.contentType + "; charset=utf-8");
    headerRes.chunked(true);
    headerRes.keep_alive(keepAlive_);
    boost::beast::http::response_serializer<boost::beast::http::empty_body> sr{headerRes};
    if (timeout_ > 0) {
        stream_.expires_after(std::chrono::seconds(timeout_));
    } else {
        size_t timeoutOneYear = 31622400;
        stream_.expires_at(std::chrono::steady_clock::now() + std::chrono::seconds(timeoutOneYear));
    }
    boost::beast::error_code ec;
    boost::beast::http::write_header(stream_, sr, ec);
    if (ec) {
        if (ec != boost::beast::http::error::end_of_stream) {
            LOG_E("[%s] [ServerConnection] Write trunk failed. Reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                ec.what().c_str());
            auto fun = serverHandler_.GetFun(ServerHandlerType::RES_ERROR);
            if (fun != nullptr) {
                fun(shared_from_this());
            }
        }
        return DoClose(ec);
    }
}

void ServerConnection::WriteChunk(const ServerRes &res)
{
    auto self(shared_from_this());
    auto& body = res.body;
    auto& isLastChunk = res.isFinish;
    if (timeout_ > 0) {
        stream_.expires_after(std::chrono::seconds(timeout_));
    } else {
        size_t timeoutOneYear = 31622400;
        stream_.expires_at(std::chrono::steady_clock::now() + std::chrono::seconds(timeoutOneYear));
    }
    boost::asio::async_write(stream_, boost::beast::http::make_chunk(GetChunk(body)),
        boost::beast::bind_front_handler(&ServerConnection::OnWriteChunk, self, isLastChunk));
}

void ServerConnection::WriteChunkTailer()
{
    auto self = shared_from_this();
    if (timeout_ > 0) {
        stream_.expires_after(std::chrono::seconds(timeout_));
    } else {
        size_t timeoutOneYear = 31622400;
        stream_.expires_at(std::chrono::steady_clock::now() + std::chrono::seconds(timeoutOneYear));
    }

    boost::asio::async_write(stream_, boost::beast::http::make_chunk_last(),
        boost::beast::bind_front_handler(&ServerConnection::OnWriteChunkTailer, self, keepAlive_));
}

void ServerConnection::OnWriteChunkTailer(bool keepAlive, boost::beast::error_code ec, size_t bytes)
{
    asyncChunkPending = false;
    boost::ignore_unused(bytes);
    if (ec) {
        if (ec != boost::beast::http::error::end_of_stream) {
            LOG_E("[%s] [ServerConnection] Write chunk tailer failed. Reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                ec.what().c_str());
            auto fun = serverHandler_.GetFun(ServerHandlerType::RES_ERROR);
            if (fun != nullptr) {
                fun(shared_from_this());
            }
        }
        return DoClose(ec);
    }
    resQueue.Clear();
    if (!keepAlive) {
        return DoClose(ec);
    }
    // 一轮chunking 结束
    chunking = false;
    // 可以开始下一次read了
    DoRead();
}

void ServerConnection::DoWriteChunk()
{
    if (resQueue.Empty()) {
        return;
    }
    asyncChunkPending = true;
    auto& res = resQueue.Front();
    if (!chunking) { // chunk的第一条消息需要发送header
        chunking = true;
        WriteChunkHeader(res);
    }

    auto& body = res.body;
    auto& isFinish = res.isFinish;

    // while the body is empty but not the last chunk message, just skip it
    if (body.empty()) {
        resQueue.PopFront();
        if (isFinish) {
            WriteChunkTailer();
        } else {
            asyncChunkPending = false;
            DoWriteChunk();
        }
        return;
    }
    WriteChunk(res);
}

void ServerConnection::OnWriteChunk(bool isFinish, boost::beast::error_code ec, size_t bytes)
{
    boost::ignore_unused(bytes);
    if (ec) {
        if (ec != boost::beast::http::error::end_of_stream) {
            LOG_E("[%s] [ServerConnection] Write trunk failed. Reason: %s.",
                GetErrorCode(ErrorType::UNREACHABLE, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                ec.what().c_str());
            auto fun = serverHandler_.GetFun(ServerHandlerType::RES_CHUNK_ERROR);
            if (fun != nullptr) {
                fun(shared_from_this());
            }
        }
        return DoClose(ec);
    }

    auto fun = serverHandler_.GetFun(ServerHandlerType::RES_CHUNK);
    if (fun != nullptr) {
        fun(shared_from_this());
    }

    // chunk消息 pop出来
    if (!resQueue.Empty()) {
        resQueue.PopFront();
    }

    if (isFinish && chunking) { // 最后一个chunk的应答
        WriteChunkTailer();
    } else {
        if (!resQueue.Empty()) {
            // 处理完前一个chunk后，下一个chunk已放入chunkResQueue中，继续处理。
            DoWriteChunk();
        } else {
            // 阶段性处理完成。下一个chunk尚未放入chunkResQueue中
            asyncChunkPending = false;
        }
    }
}

boost::asio::const_buffer ServerConnection::GetChunk(boost::beast::string_view s) const
{
    return boost::asio::const_buffer{s.data(), s.size()};
}

void ServerConnection::DoClose(boost::beast::error_code ec)
{
    if (!isClose) {
        isClose = true;
        server_.CloseOne(GetConnectionId());
        mTimer.cancel();
        try {
            stream_.socket().shutdown(boost::asio::ip::tcp::socket::shutdown_both);
        } catch (const std::exception& e) {
            LOG_E("[%s] [ServerConnection] Server connection close failed. Reason: %s.",
                GetErrorCode(ErrorType::EXCEPTION, CoordinatorFeature::SERVER_CONNECTION).c_str(),
                e.what());
        }
        if (ec && ec != boost::beast::http::error::end_of_stream) {
            auto fun = serverHandler_.GetFun(ServerHandlerType::EXCEPTION_CLOSE);
            if (fun != nullptr) {
                fun(shared_from_this());
            }
        } else {
            auto fun = serverHandler_.GetFun(ServerHandlerType::CLOSE);
            if (fun != nullptr) {
                fun(shared_from_this());
            }
        }
    }
}
}