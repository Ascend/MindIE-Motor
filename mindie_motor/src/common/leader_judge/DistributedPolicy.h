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

#ifndef MINDIE_MS_ETCD_POLICY_H
#define MINDIE_MS_ETCD_POLICY_H

#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <grpcpp/grpcpp.h>
#include "ConfigParams.h"
#include "etcd_proto/rpc.grpc.pb.h"
#include "Logger.h"

namespace MINDIE::MS {
constexpr int WATCH_MAX_RETRY = 1200; // 持续watch 6000秒

// etcd默认相关参数
constexpr int ETCD_LEASE_TTL = 20;
constexpr uint32_t CONTEXT_TIMEOUT_SECONDS = 5; // RPC超时时间（秒）
constexpr int MIN_INTERVAL = 1; // 最小间隔1秒
constexpr int MAX_RETRY = 5;
constexpr uint32_t ETCD_WATCH_GAP_SECONDS = 5;

std::string PrefixedByPodNamespace(const std::string &key);

// 默认etcdTimeInfo
struct EtcdTimeInfo {
    const int staticEtcdWatchGap;
    const int staticRpcTimeout;
    const int staticLeaseTtl;

    // 默认EtcdTimeInfo，单次续约20s，间隔一半时间再次续约，间隔5秒监控锁状态
    EtcdTimeInfo()
        : staticEtcdWatchGap(ETCD_WATCH_GAP_SECONDS),
          staticRpcTimeout(CONTEXT_TIMEOUT_SECONDS),
          staticLeaseTtl(ETCD_LEASE_TTL) {}

    // 可定制化EtcdTimeInfo
    EtcdTimeInfo(int staticEtcdWatchGap, int staticRpcTimeout, int staticLeaseTtl)
        : staticEtcdWatchGap(staticEtcdWatchGap),
          staticRpcTimeout(staticRpcTimeout),
          staticLeaseTtl(staticLeaseTtl) {}
};

class DistributedLockPolicy {
public:
    virtual ~DistributedLockPolicy() = default;
    virtual bool TryLock() = 0;
    virtual void Unlock() = 0;
    virtual void RegisterCallBack(std::function<void(bool)> callback) = 0;
    virtual bool SafePut(const std::string& key, const std::string& value) = 0;
    virtual bool GetWithRevision(const std::string& key, std::string& value) = 0;
};

class EtcdDistributedLock : public DistributedLockPolicy {
public:
    EtcdDistributedLock(const std::string& etcdAddr,
                       const std::string& lockKey,
                       const std::string& clientId,
                       TlsItems& tlsConfig,
                       EtcdTimeInfo etcdTimeInfo);

#ifdef UT_FLAG
    // 测试环境构造函数（仅测试时启用）
    EtcdDistributedLock(
        std::shared_ptr<etcdserverpb::KV::StubInterface> kvStub,
        std::shared_ptr<etcdserverpb::Lease::StubInterface> leaseStub,
        std::string& lockKeyRef,
        std::string& clientIdRef,
        EtcdTimeInfo etcdTimeInfo);

    bool IsLocked();
    void SetLock(bool flag);
#endif // UT_FLAG

    ~EtcdDistributedLock() override;

    bool TryLock() override;
    void Unlock() override;
    void RegisterCallBack(std::function<void(bool)> callback) override;
    bool SafePut(const std::string& key, const std::string& value) override;
    bool GetWithRevision(const std::string& key, std::string& value) override;

private:
    struct TlsConfig;
    
    bool AcquireLockOnce();
    bool TryLockOnce();
    void InitializeEtcdClient(const std::string& etcdAddr, TlsItems& tlsConfig);
    bool CreateLease();
    void RevokeLease();
    void StartLeaseKeepAlive();
    void LeaseKeepAliveWorker();
    void StartWatch();
    void WatchWorker();
    void HandleLeaseLost();
    void HandleLockChange(bool newLockState);
    void NotifyLockChange(bool locked);
    void Stop();
    int64_t GetCurrentModVer(const std::string& key);
    int CalculateSleepTime(int ttl) const;
    void BuildLockTxnRequest(etcdserverpb::TxnRequest& txn_req);
    bool ExecuteTxnRequest(const etcdserverpb::TxnRequest& txn_req,
        etcdserverpb::TxnResponse& txn_resp);
    void HandleLockConflict(const etcdserverpb::TxnResponse& txn_resp);
    void HandleLockAcquired(const etcdserverpb::TxnResponse& txn_resp);
    bool TryRenewLease(int& retryCount, int& ttl);

    std::string clientId;
    std::string lockKey;
    int leaseTtl;
    EtcdTimeInfo etcdTimeInfo;
    int64_t leaseId = 0;
    int64_t lastObservedRevision = 0;

    std::shared_ptr<grpc::Channel> channel_;
#ifdef UT_FLAG
    std::shared_ptr<etcdserverpb::KV::StubInterface> kv_stub_;
    std::shared_ptr<etcdserverpb::Lease::StubInterface> lease_stub_;
#else
    std::shared_ptr<etcdserverpb::KV::Stub> kv_stub_;
    std::shared_ptr<etcdserverpb::Lease::Stub> lease_stub_;
#endif // UT_FLAG
    std::atomic<bool> running_{true};
    std::atomic<bool> isLocked{false};

    std::unique_ptr<std::thread> watchThreadPtr;
    std::unique_ptr<std::thread> keepaliveThreadPtr;

    std::mutex mutex_;
    std::mutex cbMutex;
    std::function<void(bool)> callback_;
};

class LeaseKeepAliveSession {
public:
    std::unique_ptr<grpc::ClientContext> context;
    std::unique_ptr<grpc::ClientReaderWriterInterface<etcdserverpb::LeaseKeepAliveRequest,
                etcdserverpb::LeaseKeepAliveResponse>> stream;

    explicit LeaseKeepAliveSession(etcdserverpb::Lease::StubInterface& stub, int staticLeaseTtl)
    {
        context = std::make_unique<grpc::ClientContext>();
        if (context != nullptr) {
            context->set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(staticLeaseTtl));
            stream = stub.LeaseKeepAlive(context.get());
        }
    }

    ~LeaseKeepAliveSession()
    {
        if (stream) {
            stream->WritesDone();
            auto status = stream->Finish();
            if (!status.ok()) {
                LOG_E("Stream cleanup error: %s", status.error_message().c_str());
            }
        }
    }

    bool WriteRequest(int64_t leaseId)
    {
        if (stream == nullptr) {
            LOG_E("[WriteRequest] fail, stream is nullptr!");
            return false;
        }
        etcdserverpb::LeaseKeepAliveRequest req;
        req.set_id(leaseId);
        return stream->Write(req);
    }

    bool ReadResponse(etcdserverpb::LeaseKeepAliveResponse* resp)
    {
        if (stream == nullptr) {
            LOG_E("[ReadResponse] fail, stream is nullptr!");
            return false;
        }
        return stream->Read(resp);
    }
};

} // namespace MINDIE::MS

#endif // MINDIE_MS_ETCD_POLICY_H
