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
#ifndef CONCURRENT_MAP_H
#define CONCURRENT_MAP_H
#include <unordered_map>
#include <pthread.h>
#include <optional>
#include <vector>
#include <algorithm>

namespace MINDIE {
namespace MS {
template <typename K, typename V> class ConcurrentMap {
public:
    ConcurrentMap() { pthread_spin_init(&spinlock_, PTHREAD_PROCESS_PRIVATE); }

    ~ConcurrentMap() { pthread_spin_destroy(&spinlock_); }

    // 插入键值对, key已存在时不覆盖. 返回true表示新插入, false表示key已存在
    bool Insert(const K &key, const V &value)
    {
        pthread_spin_lock(&spinlock_);
        auto result = map_.insert(std::make_pair(key, value));
        pthread_spin_unlock(&spinlock_);
        return result.second;
    }

    void Erase(const K &key)
    {
        pthread_spin_lock(&spinlock_);
        map_.erase(key);
        pthread_spin_unlock(&spinlock_);
    }

    void Set(const K &key, const V &value)
    {
        pthread_spin_lock(&spinlock_);
        map_[key] = value;
        pthread_spin_unlock(&spinlock_);
    }

    size_t Count(const K &key) const
    {
        pthread_spin_lock(&spinlock_);
        size_t result = map_.count(key);
        pthread_spin_unlock(&spinlock_);
        return result;
    }

    std::optional<V> Get(const K &key) const
    {
        pthread_spin_lock(&spinlock_);
        auto it = map_.find(key);
        std::optional<V> result;
        if (it != map_.end()) {
            result = it->second;
        }
        pthread_spin_unlock(&spinlock_);
        return result;
    }

    size_t Size() const
    {
        pthread_spin_lock(&spinlock_);
        size_t result = map_.size();
        pthread_spin_unlock(&spinlock_);
        return result;
    }

    std::vector<K> KeySet() const
    {
        pthread_spin_lock(&spinlock_);
        std::vector<K> keys;
        for (const auto &pair : map_) {
            keys.push_back(pair.first);
        }
        pthread_spin_unlock(&spinlock_);
        return keys;
    }

    std::vector<V> Values() const
    {
        std::vector<V> result;
        pthread_spin_lock(&spinlock_);
        std::transform(
            map_.begin(), map_.end(),
            std::back_inserter(result),
            [](const auto& pair) { return pair.second; }
        );
        pthread_spin_unlock(&spinlock_);
        return result;
    }
    // 仅用于标量值的递增
    void IncValue(const K &key)
    {
        pthread_spin_lock(&spinlock_);
        auto it = map_.find(key);
        if (it != map_.end()) {
            map_[key]++;
        } else {
            map_.insert(std::make_pair(key, 1));
        }
        pthread_spin_unlock(&spinlock_);
    }
    // 仅用于标量值的递增
    void DecValue(const K &key)
    {
        pthread_spin_lock(&spinlock_);
        map_[key]--;
        pthread_spin_unlock(&spinlock_);
    }

private:
    std::unordered_map<K, V> map_;
    mutable pthread_spinlock_t spinlock_;
};
} // namespace MS
} // namespace MINDIE
#endif