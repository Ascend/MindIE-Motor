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

#include <chrono>
#include <condition_variable>
#include <fstream>
#include <sstream>
#include <thread>
#include <atomic>
#include "ConfigParams.h"
#include "KmcSecureString.h"
#include "KmcDecryptor.h"
#include "GrpcClusterClient.h"
#include "DistributedPolicy.h"

namespace MINDIE {
namespace MS {
// 获取POD_NAMESPACE作为操作etcd的key前缀,如果要新增etcd的key,请以/开头,这样拼接后的key格式为/namespace/xx,是etcd中key的推荐格式
std::string PrefixedByPodNamespace(const std::string &key)
{
    const char* podNamespace = std::getenv("POD_NAMESPACE");
    // 环境变量未设置或未空,则不处理
    if (podNamespace == nullptr || std::strlen(podNamespace) == 0) {
        return key;
    }
    return "/" + std::string(podNamespace) + key;
}

// --------------------------
// 构造函数与析构函数
// --------------------------
EtcdDistributedLock::EtcdDistributedLock(const std::string& etcdAddr,
                                         const std::string& lockKey,
                                         const std::string& clientId,
                                         TlsItems& tlsConfig,
                                         EtcdTimeInfo etcdTimeInfo)
    : clientId(clientId),
      lockKey(PrefixedByPodNamespace(lockKey)),
      leaseTtl(etcdTimeInfo.staticLeaseTtl),
      etcdTimeInfo(etcdTimeInfo) {
    InitializeEtcdClient(etcdAddr, tlsConfig);
}

// 新增构造函数（测试环境使用）
#ifdef UT_FLAG
EtcdDistributedLock::EtcdDistributedLock(
    std::shared_ptr<etcdserverpb::KV::StubInterface> kvStub,
    std::shared_ptr<etcdserverpb::Lease::StubInterface> leaseStub,
    std::string& lockKeyRef,
    std::string& clientIdRef,
    EtcdTimeInfo etcdTimeInfoRef)
    : leaseTtl(etcdTimeInfoRef.staticLeaseTtl),
      etcdTimeInfo(etcdTimeInfoRef)
{
    kv_stub_ = std::static_pointer_cast<etcdserverpb::KV::StubInterface>(kvStub);
    lease_stub_ = std::static_pointer_cast<etcdserverpb::Lease::StubInterface>(leaseStub);

    clientId = clientIdRef;
    lockKey = PrefixedByPodNamespace(lockKeyRef);
}

bool EtcdDistributedLock::IsLocked()
{
    return isLocked.load();
}

void EtcdDistributedLock::SetLock(bool flag)
{
    isLocked.store(flag);
}
#endif // UT_FLAG

EtcdDistributedLock::~EtcdDistributedLock()
{
    Stop();
}


void EtcdDistributedLock::RegisterCallBack(std::function<void(bool)> callback)
{
    std::lock_guard<std::mutex> lock(cbMutex);
    callback_ = callback;
}

bool EtcdDistributedLock::AcquireLockOnce()
{
    LOG_D("[LeaderAgent AcquireLockOnce] start lock once");
    if (isLocked.load()) {
        return true;
    }

    if (!CreateLease()) {
        LOG_E("[AcquireLock] Failed to create lease");
        return false;
    }

    etcdserverpb::TxnRequest txn_req;
    BuildLockTxnRequest(txn_req);

    etcdserverpb::TxnResponse txn_resp;
    if (!ExecuteTxnRequest(txn_req, txn_resp)) {
        return false;
    }

    if (!txn_resp.succeeded()) {
        HandleLockConflict(txn_resp);
        return false;
    }
    HandleLockAcquired(txn_resp);
    return true;
}

void EtcdDistributedLock::BuildLockTxnRequest(etcdserverpb::TxnRequest& txn_req)
{
    // 构造比较条件（检查锁是否存在）
    auto* compare = txn_req.add_compare();
    if (compare != nullptr) {
        compare->set_key(lockKey);
        compare->set_target(etcdserverpb::Compare::CREATE);
        compare->set_create_revision(0);
    }

    // 成功分支：创建新锁
    auto* success = txn_req.add_success();
    if (success != nullptr) {
        auto* put_op = success->mutable_request_put();
        if (put_op != nullptr) {
            put_op->set_key(lockKey);
            put_op->set_value(clientId);
            put_op->set_lease(leaseId);
        }
    }

    // 失败分支：获取当前锁信息
    auto* failure = txn_req.add_failure();
    if (failure != nullptr) {
        auto* range_op = failure->mutable_request_range();
        if (range_op != nullptr) {
            range_op->set_key(lockKey);
        }
    }
}

bool EtcdDistributedLock::ExecuteTxnRequest(const etcdserverpb::TxnRequest& txn_req,
    etcdserverpb::TxnResponse& txn_resp)
{
    if (kv_stub_ == nullptr) {
        LOG_E("[ExecuteTxnRequest] failed: kv_stub_ is nullptr!");
        return false;
    }
    grpc::ClientContext context;
    context.set_deadline(std::chrono::system_clock::now() +
                        std::chrono::seconds(etcdTimeInfo.staticRpcTimeout));

    const auto status = kv_stub_->Txn(&context, txn_req, &txn_resp);
    if (!status.ok()) {
        LOG_E("[AcquireLock] RPC failed: %s (code: %d)",
              status.error_message().c_str(), status.error_code());
        return false;
    }
    return true;
}
void EtcdDistributedLock::HandleLockConflict(const etcdserverpb::TxnResponse& txn_resp)
{
    LOG_D("[AcquireLock] Lock contention detected");
    const auto& range_response = txn_resp.responses(0).response_range();

    if (range_response.kvs_size() > 0) {
        const auto& kv = range_response.kvs(0);
        lastObservedRevision = kv.mod_revision();
        LOG_I("[AcquireLock] Lock held by %s (revision=%ld), lease %ld",
              kv.value().c_str(), lastObservedRevision, kv.lease());
    }
}

void EtcdDistributedLock::HandleLockAcquired(const etcdserverpb::TxnResponse& txn_resp)
{
    for (const auto& response_op : txn_resp.responses()) {
        if (response_op.has_response_put()) {
            lastObservedRevision = response_op.response_put().header().revision();
            LOG_I("[AcquireLock] Lock acquired (revision: %ld)", lastObservedRevision);
            break;
        }
    }
}

// --------------------------
// 锁状态变更处理
// --------------------------
void EtcdDistributedLock::HandleLockChange(bool newLockState)
{
    const bool oldState = isLocked.exchange(newLockState);
    if (oldState == newLockState) {return;}
    // 记录状态变更日志
    LOG_I("[LockState] Changed from %s to %s",
          oldState ? "LOCKED" : "UNLOCKED",
          newLockState ? "LOCKED" : "UNLOCKED");
    // 触发回调通知
    NotifyLockChange(newLockState);
}

bool EtcdDistributedLock::TryLockOnce()
{
    if (AcquireLockOnce()) { // 成功获取锁
        HandleLockChange(true);
        StartLeaseKeepAlive(); // 持续续约
        return true;
    } else {
        HandleLockChange(false);
        return false;
    }
}

bool EtcdDistributedLock::TryLock()
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (isLocked.load()) {
        return true; // 已持有锁
    }
    auto ret = TryLockOnce();
    // 启动监听线程
    StartWatch();
    return ret;
}

void EtcdDistributedLock::Unlock()
{
    if (isLocked.exchange(false)) {
        RevokeLease();
        LOG_I("[EtcdLock] Released lock successfully");
    }
}

// GetCurrentModVer方法仅在SafePut使用,key的namespace前缀在SafePut中添加
int64_t EtcdDistributedLock::GetCurrentModVer(const std::string& key)
{
    if (kv_stub_ == nullptr) {
        LOG_E("[GetCurrentModVer] failed: kv_stub_ is nullptr!");
        return 0;
    }
    etcdserverpb::RangeRequest req;
    req.set_key(key);
    grpc::ClientContext context;
    etcdserverpb::RangeResponse resp;
    const auto status = kv_stub_->Range(&context, req, &resp);
    if (status.ok() && resp.kvs_size() > 0) {
        return resp.kvs(0).mod_revision();
    }
    return 0;
}

// --------------------------
// 数据操作接口
// --------------------------
bool EtcdDistributedLock::SafePut(const std::string& key, const std::string& value)
{
    std::string prefixedKey = PrefixedByPodNamespace(key);
    auto expectedRevision = GetCurrentModVer(prefixedKey);
    etcdserverpb::TxnRequest txn_req;
    auto* compare = txn_req.add_compare();
    if (compare != nullptr) {
        compare->set_key(prefixedKey);
        compare->set_target(etcdserverpb::Compare::MOD);
        compare->set_mod_revision(expectedRevision);
    }

    auto* success_op = txn_req.add_success();
    if (success_op != nullptr) {
        auto* put_op = success_op->mutable_request_put();
        if (put_op != nullptr) {
            put_op->set_key(prefixedKey);
            put_op->set_value(value);
        }
    }

    etcdserverpb::TxnResponse txn_resp;
    grpc::ClientContext context;
    context.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(etcdTimeInfo.staticRpcTimeout));

    if (kv_stub_ == nullptr) {
        LOG_E("[EtcdDistributedLock] SafePut failed, kv_stub_ is nullptr!");
        return false;
    }
    const auto status = kv_stub_->Txn(&context, txn_req, &txn_resp);
    if (!status.ok()) {
        LOG_E("[EtcdDistributedLock] SafePut RPC error: %s", status.error_message().c_str());
        return false;
    }

    if (txn_resp.succeeded()) {
        LOG_I("[EtcdDistributedLock] SafePut succeeded at revision:%ld", expectedRevision + 1);
        return true;
    } else {
        LOG_E("[EtcdDistributedLock] SafePut conflict detected");
        return false;
    }
}

bool EtcdDistributedLock::GetWithRevision(const std::string& key, std::string& value)
{
    etcdserverpb::RangeRequest req;
    req.set_key(PrefixedByPodNamespace(key));
    
    grpc::ClientContext context;
    context.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(etcdTimeInfo.staticRpcTimeout));
    
    etcdserverpb::RangeResponse resp;
    if (kv_stub_ == nullptr) {
        LOG_E("[GetWithRevision] failed, kv_stub_ is nullptr!");
        return false;
    }
    const auto status = kv_stub_->Range(&context, req, &resp);
    if (status.ok() && resp.count() > 0) {
        value = resp.kvs(0).value();
        LOG_D("[GetWithRevision] Retrieved value at revision:%ld", resp.kvs(0).mod_revision());
        return true;
    }
    return false;
}

// --------------------------
// 网络连接管理
// --------------------------
void EtcdDistributedLock::InitializeEtcdClient(const std::string& etcdAddr, TlsItems& tlsConfig)
{
    channel_ = CreateGrpcChannel(etcdAddr, tlsConfig);
    if (!channel_) {
        LOG_E("[%s] [EtcdDistributedLock] Failed to initialize etcd client channel, ADDR:%s",
            GetErrorCode(ErrorType::UNAUTHENTICATED, ControllerFeature::LEADER_AGENT).c_str(), etcdAddr.c_str());
        return;
    }
    kv_stub_ = etcdserverpb::KV::NewStub(channel_);
    lease_stub_ = etcdserverpb::Lease::NewStub(channel_);
    LOG_I("[InitializeEtcdClient] Connected to etcd cluster at:%s", etcdAddr.c_str());
}

// --------------------------
// 租约与监听管理
// --------------------------
bool EtcdDistributedLock::CreateLease()
{
    etcdserverpb::LeaseGrantRequest req;
    req.set_ttl(leaseTtl);
    
    etcdserverpb::LeaseGrantResponse resp;
    grpc::ClientContext context;
    context.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(etcdTimeInfo.staticRpcTimeout));
    if (!lease_stub_) {
        return false;
    }
    const auto status = lease_stub_->LeaseGrant(&context, req, &resp);
    if (!status.ok()) {
        LOG_E("[CreateLease] Failed:%s", status.error_message().c_str());
        return false;
    }
    
    leaseId = resp.id();
    LOG_I("[CreateLease] New lease ID:%ld with TTL:%d", leaseId, leaseTtl);
    return true;
}

void EtcdDistributedLock::RevokeLease()
{
    if (leaseId == 0) {
        return;
    }
    
    etcdserverpb::LeaseRevokeRequest req;
    req.set_id(leaseId);

    etcdserverpb::LeaseRevokeResponse resp;
    grpc::ClientContext context;
    context.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(etcdTimeInfo.staticRpcTimeout));
    if (lease_stub_ == nullptr) {
        LOG_E("[RevokeLease] failed, lease_stub_ is nullptr!, leaseId reset!");
        leaseId = 0;
        return;
    }
    lease_stub_->LeaseRevoke(&context, req, &resp);
    LOG_I("[RevokeLease] Lease:%ld revoked", leaseId);
    leaseId = 0;
}

int EtcdDistributedLock::CalculateSleepTime(int ttl) const
{
    if (ttl <= 0) {return MIN_INTERVAL;}
    return std::clamp(ttl / 2, MIN_INTERVAL, etcdTimeInfo.staticLeaseTtl / 2); // 2：租约剩余时间二分之一
}

bool EtcdDistributedLock::TryRenewLease(int& retryCount, int& ttl)
{
    if (lease_stub_ == nullptr) {
        LOG_E("[TryRenewLease] failed, lease_stub_ is nullptr!");
        return false;
    }
    LeaseKeepAliveSession session(*lease_stub_.get(), etcdTimeInfo.staticLeaseTtl);
    
    if (!session.WriteRequest(leaseId)) {
        LOG_E("[TryRenewLease]Write failed, retry:%d", retryCount);
        return false;
    }

    etcdserverpb::LeaseKeepAliveResponse resp;
    if (!session.ReadResponse(&resp)) {
        LOG_E("[TryRenewLease]Read failed, retry:%d", retryCount);
        return false;
    }

    if (resp.id() != leaseId || resp.ttl() <= 0) { // 对返回值做合法性校验
        LOG_E("[TryRenewLease]Invalid lease ID or ttl received:%ld、%ld", resp.id(), resp.ttl());
        return false;
    }
    ttl = resp.ttl();
    LOG_I("[TryRenewLease]Renewed lease:%ld, TTL:%d", resp.id(), resp.ttl());
    return true;
}

void EtcdDistributedLock::StartLeaseKeepAlive()
{
    running_.store(false);
    if (keepaliveThreadPtr && keepaliveThreadPtr->joinable()) {
        LOG_W("LeaseKeepAlive thread is already running");
        keepaliveThreadPtr->join();
        keepaliveThreadPtr.reset();
    }
    running_.store(true);

    try {
        keepaliveThreadPtr = std::make_unique<std::thread>(&EtcdDistributedLock::LeaseKeepAliveWorker, this);
    } catch (const std::exception &e) {
        LOG_E("[StartLeaseKeepAlive] Failed to create keepalive thread: {}", e.what());
        running_.store(false);
        keepaliveThreadPtr.reset();
        throw std::runtime_error("Failed to create keepalive thread");
    }
}

void EtcdDistributedLock::LeaseKeepAliveWorker()
{
    int retryCount = 0;
    int ttl = 0;
    try {
        while (running_.load() && retryCount < MAX_RETRY) {
            if (!TryRenewLease(retryCount, ttl)) {
                (retryCount)++;
                std::this_thread::sleep_for(std::chrono::seconds(1));
                continue;
            }
            retryCount = 0; // 成功后续约计数清零
            int sleepSec = CalculateSleepTime(ttl);
            std::this_thread::sleep_for(std::chrono::seconds(sleepSec));
        }

        HandleLeaseLost();
        LOG_W("LeaseKeepAlive thread exited");
    } catch (const std::exception& e) {
        LOG_E("LeaseKeepAlive worker exception: {}", e.what());
        HandleLeaseLost();
    }
}

void EtcdDistributedLock::StartWatch()
{
    try {
        watchThreadPtr = std::make_unique<std::thread>(&EtcdDistributedLock::WatchWorker, this);
    } catch (const std::exception &e) {
        LOG_E("[StartWatch] Failed to create watch thread: {}", e.what());
        watchThreadPtr.reset();
        throw std::runtime_error("Failed to create watch thread");
    }
}

void EtcdDistributedLock::WatchWorker()
{
    int retryCount = 0;
    try {
        while (retryCount < WATCH_MAX_RETRY) {
            etcdserverpb::RangeRequest range_req;
            range_req.set_key(lockKey);
            range_req.set_limit(1); // 1：表示只获取一个键值对
            etcdserverpb::RangeResponse range_resp;
            grpc::ClientContext kv_ctx;
            if (kv_stub_ == nullptr || !kv_stub_->Range(&kv_ctx, range_req, &range_resp).ok()) {
                LOG_E("[LeaderAgent StartWatch] Range failed, retry:%d", retryCount);
                retryCount++;
                std::this_thread::sleep_for(std::chrono::seconds(ETCD_WATCH_GAP_SECONDS));
                continue;
            }
            retryCount = 0;
            if (range_resp.kvs_size() > 0) {
                LOG_I("[LeaderAgent StartWatch] Lock is locked, it means leader alive, keep watch");
                std::this_thread::sleep_for(std::chrono::seconds(ETCD_WATCH_GAP_SECONDS));
                continue;
            }
            if (!isLocked.load()) {
                auto ret = TryLockOnce();
                LOG_I("[LeaderAgent StartWatch] Lock is unlock, follwer try to lock, ret is:%d", ret);
            } else {
                LOG_W("[LeaderAgent StartWatch] Lock is unlock, leader change to follwer");
                NotifyLockChange(false);
            }
            std::this_thread::sleep_for(std::chrono::seconds(ETCD_WATCH_GAP_SECONDS));
        }
        LOG_W("[LeaderAgent StartWatch] StartWatch thread exited");
    } catch (const std::exception &e) {
        LOG_E("StartWatch worker exception: {}", e.what());
    }
}

// --------------------------
// 状态变更处理
// --------------------------
void EtcdDistributedLock::HandleLeaseLost()
{
    if (isLocked.exchange(false)) {
        LOG_W("[LeaderAgent LeaseLost] Lease expired, releasing lock");
        NotifyLockChange(false);
    }
}

void EtcdDistributedLock::NotifyLockChange(bool locked)
{
    std::lock_guard<std::mutex> lock(cbMutex);
    if (callback_) {
        LOG_I("[LeaderAgent Notify] Lock state changed to:%s", locked ? "Leader" : "Follower");
        callback_(locked);
    }
}

void EtcdDistributedLock::Stop()
{
    running_.store(false);
    
    if (keepaliveThreadPtr && keepaliveThreadPtr->joinable()) {
        keepaliveThreadPtr->join();
    }
    if (watchThreadPtr && watchThreadPtr->joinable()) {
        watchThreadPtr->join();
    }
    
    if (isLocked.load()) {
        RevokeLease();
    }
    LOG_I("[Stop] Etcd lock service stopped");
}
} // namespace MS
} // namespace MINDIE
