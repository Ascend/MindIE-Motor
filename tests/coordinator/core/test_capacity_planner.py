# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest

from motor.coordinator.metrics.capacity_planner import (
    PlannerConfig,
    PlannerSnapshot,
    RoleSnapshot,
    CapacityPlanner,
)


def _role(req=0.0, tokens=0.0, tps=None, instances=1, **kw):
    return RoleSnapshot(
        active_instances=instances,
        instance_tps=tps or [],
        request_success_total=req,
        tokens_total=tokens,
        **kw,
    )


def test_lambda_and_avg_length_from_deltas():
    p = CapacityPlanner(PlannerConfig())
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=100, tokens=50000), decode=None))
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=130, tokens=65000), decode=None))
    out = p.compute()
    # λ 样本 = 30/3 = 10 rps（EMA 首个样本即值）；L̄_in 样本 = 15000/30 = 500
    assert abs(out["prefill_demand_tps"] - 5000.0) < 1e-6


def test_zero_delta_holds_previous_values():
    p = CapacityPlanner(PlannerConfig())
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=100, tokens=50000), decode=None))
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=130, tokens=65000), decode=None))
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=130, tokens=65000), decode=None))  # 零增量
    out = p.compute()
    assert abs(out["prefill_demand_tps"] - 5000.0) < 1e-6  # 保持，不归零


def test_negative_delta_clamped_on_scale_in():
    p = CapacityPlanner(PlannerConfig())
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=100, tokens=50000), decode=None))
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=20, tokens=10000), decode=None))  # 缩容跌计数
    out = p.compute()
    assert out["prefill_demand_tps"] == 0.0  # 负 delta clamp，无样本 → 需求 0


def test_negative_token_delta_holds_length_but_updates_lambda():
    # 显式锁定 α=0.3：本用例验证的是 EMA 混合机制本身，数值链与默认值解耦
    p = CapacityPlanner(PlannerConfig(ema_alpha=0.3))
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=100, tokens=50000), decode=None))
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=130, tokens=65000), decode=None))
    # req_delta = 10 > 0 → λ 样本 = 10/3 正常更新；token_delta = -25000 <= 0 → L̄ 保持 500，不进负样本
    p.update(PlannerSnapshot(dt=3.0, prefill=_role(req=140, tokens=40000), decode=None))
    out = p.compute()
    # λ EMA = 10 + 0.3 × (10/3 - 10) = 8；L̄ EMA 保持 500 → 需求 = 8 × 500 = 4000
    assert abs(out["prefill_demand_tps"] - 4000.0) < 1e-6


def test_cp_from_tokens_over_prefill_time():
    cfg = PlannerConfig()
    p = CapacityPlanner(cfg)

    def snap(tok, pts):
        return PlannerSnapshot(dt=3.0, decode=None, prefill=_role(req=10, tokens=tok, prefill_time_sum=pts))

    p.update(snap(50000, 10.0))
    p.update(snap(65000, 13.0))  # 样本 = 15000/3.0 = 5000 tokens/s
    out = p.compute()
    assert abs(out["prefill_capacity_tps"] - 5000.0) < 1e-6
    assert out["capacity_calibrated_prefill"] == 1.0


def test_cp_sample_dropped_when_time_delta_zero():
    p = CapacityPlanner(PlannerConfig())
    p.update(
        PlannerSnapshot(
            dt=3.0,
            decode=None,
            prefill=_role(req=10, tokens=50000, prefill_time_sum=10.0),
        )
    )
    p.update(
        PlannerSnapshot(
            dt=3.0,
            decode=None,
            prefill=_role(req=12, tokens=66000, prefill_time_sum=10.0),
        )
    )  # 时间零增量
    out = p.compute()
    assert out["prefill_capacity_tps"] == 0.0  # 丢样，未标定
    assert out["capacity_calibrated_prefill"] == 0.0


def test_cd_peak_learning_and_decay():
    cfg = PlannerConfig(capacity_decay=0.1, capacity_window_cycles=2)  # 大衰减 + 小窗口便于测试
    p = CapacityPlanner(cfg)
    p.update(PlannerSnapshot(dt=3.0, prefill=None, decode=_role(instances=2, tps=[100.0, 200.0])))
    assert p.compute()["decode_capacity_tps"] == 200.0  # max 立即采纳
    p.update(PlannerSnapshot(dt=3.0, prefill=None, decode=_role(instances=2, tps=[50.0, 60.0])))
    assert p.compute()["decode_capacity_tps"] == 200.0  # 峰值 200 仍在窗口内，floor 顶住衰减
    p.update(PlannerSnapshot(dt=3.0, prefill=None, decode=_role(instances=2, tps=[50.0, 60.0])))
    assert abs(p.compute()["decode_capacity_tps"] - 180.0) < 1e-6  # 200 滑出窗口，200*(1-0.1)
    p.update(PlannerSnapshot(dt=3.0, prefill=None, decode=_role(instances=2, tps=[500.0, 60.0])))
    assert p.compute()["decode_capacity_tps"] == 500.0  # 更高观测再上抬


def test_cd_decay_floor_steady_low_load():
    # 稳态低载：峰值 2000 滑出窗口后 C 随衰减下行，收敛到窗口峰值 floor=50 后停止下探
    p = CapacityPlanner(PlannerConfig(capacity_decay=0.1, capacity_window_cycles=50))
    counters = {"req": 0.0, "tokens": 0.0}

    def cycle(tps):
        counters["req"] += 10
        counters["tokens"] += 500
        p.update(
            PlannerSnapshot(
                dt=3.0,
                prefill=None,
                decode=_role(req=counters["req"], tokens=counters["tokens"], tps=[tps]),
            )
        )

    cycle(2000.0)  # 建立峰值
    assert p.compute()["decode_capacity_tps"] == 2000.0
    for _ in range(300):
        cycle(50.0)
    out = p.compute()
    assert out["decode_capacity_tps"] == 50.0  # 收敛到 floor，而非继续下探到 0
    last_required = out["decode_replicas_required"]
    for _ in range(5):
        cycle(50.0)
    out = p.compute()
    assert out["decode_capacity_tps"] == 50.0
    assert out["decode_replicas_required"] == last_required  # required 停止增长


def test_cd_decay_floor_window_forgetting():
    # 窗口遗忘：旧峰值在窗口内时 floor 顶住衰减；滑出后 C 随衰减降至新窗口峰值后停止
    p = CapacityPlanner(PlannerConfig(capacity_decay=0.1, capacity_window_cycles=10))

    def snap(tps):
        return PlannerSnapshot(dt=3.0, prefill=None, decode=_role(tps=[tps]))

    p.update(snap(2000.0))
    assert p.compute()["decode_capacity_tps"] == 2000.0
    for _ in range(9):
        p.update(snap(100.0))
    assert p.compute()["decode_capacity_tps"] == 2000.0  # 2000 仍在窗口内
    p.update(snap(100.0))  # 2000 滑出窗口
    assert abs(p.compute()["decode_capacity_tps"] - 1800.0) < 1e-6  # 衰减恢复：2000*(1-0.1)
    for _ in range(60):
        p.update(snap(100.0))
    assert p.compute()["decode_capacity_tps"] == 100.0  # 降至新窗口峰值后停止


def test_cd_empty_tps_holds_and_decode_prior_floor():
    p = CapacityPlanner(PlannerConfig(decode_tps_capacity_prior=1500.0))
    assert p.compute()["decode_capacity_tps"] == 1500.0  # prior 起步
    assert p.compute()["capacity_calibrated_decode"] == 1.0
    p.update(PlannerSnapshot(dt=3.0, prefill=None, decode=_role(tps=[100.0])))
    assert p.compute()["decode_capacity_tps"] == 1500.0  # 低样本不破 prior floor
    p2 = CapacityPlanner(PlannerConfig(capacity_decay=0.1, capacity_window_cycles=2))
    p2.update(PlannerSnapshot(dt=3.0, prefill=None, decode=_role(tps=[200.0])))
    assert p2.compute()["decode_capacity_tps"] == 200.0
    for _ in range(5):
        p2.update(PlannerSnapshot(dt=3.0, prefill=None, decode=_role(tps=[])))
    assert p2.compute()["decode_capacity_tps"] == 200.0  # 空 instance_tps 无样本，保持不衰减


def test_capacity_priors():
    p = CapacityPlanner(PlannerConfig(prefill_tps_capacity_prior=8000.0))
    out = p.compute()
    assert out["prefill_capacity_tps"] == 8000.0
    assert out["capacity_calibrated_prefill"] == 1.0


def _loaded_planner(cfg=None, **over):
    """构造已标定 planner：C_p=5000, C_d=2000, λ=10, L̄_in=500, L̄_out=200。"""
    p = CapacityPlanner(cfg or PlannerConfig())
    kw = dict(tokens=0, prefill_time_sum=0.0)
    p.update(
        PlannerSnapshot(
            dt=1.0,
            prefill=_role(req=0, instances=1, **kw),
            decode=_role(req=0, instances=1, tps=[0.0], **{}),
        )
    )
    p.update(
        PlannerSnapshot(
            dt=1.0,
            prefill=_role(req=10, tokens=5000, prefill_time_sum=1.0, instances=1),
            decode=_role(
                req=10,
                tokens=2000,
                tps=[2000.0],
                instances=1,
                queue_time_sum=0.0,
                queue_time_count=10,
                decode_time_sum=40.0,
                decode_time_count=10,
                kv_tokens_per_instance_decode=100000,
            ),
        )
    )
    return p


def test_required_instances_throughput_bound():
    out = _loaded_planner().compute()
    # D_p = 10*500 = 5000 → N_p = ceil(5000/(5000*0.8)) = 2
    assert out["prefill_replicas_required"] == 2.0
    # D_d = 10*200 = 2000 → N_d_tp = ceil(2000/(2000*0.8)) = 2
    # W_kv = 0 + 4 = 4s → D_kv = 10*4*700 = 28000 → N_d_kv = ceil(28000/90000) = 1
    assert out["decode_replicas_required"] == 2.0  # max(2,1)
    assert out["pd_ratio_required_raw"] == 1.0


def test_kv_constraint_binding():
    # 显式锁定 α=0.3：W_kv EMA 收敛数值链（122.8 → 205.96）依赖旧系数，
    # 本用例测的是 KV 约束 binding 机制而非默认值
    p = _loaded_planner(PlannerConfig(ema_alpha=0.3))
    # 累计计数器推进 → decode 均值样本 = (4040-40)/(20-10) = 400s，queue 分量 Δsum=0 保持 0
    # W_kv EMA = 4 + 0.3*(400-4) = 122.8 → D_kv = 10*122.8*700 = 859600
    # → N_d_kv = ceil(859600/90000) = 10（> 吞吐约束 2，KV binding）
    p.update(
        PlannerSnapshot(
            dt=1.0,
            prefill=_role(req=10, tokens=5000, prefill_time_sum=1.0, instances=1),
            decode=_role(
                req=10,
                tokens=2000,
                tps=[2000.0],
                instances=1,
                queue_time_sum=0.0,
                queue_time_count=20,
                decode_time_sum=4040.0,
                decode_time_count=20,
                kv_tokens_per_instance_decode=100000,
            ),
        )
    )
    out = p.compute()
    assert out["decode_replicas_required"] == 10.0
    assert out["kv_demand_tokens"] > 0
    # 样本仍为 400 → W_kv EMA = 122.8 + 0.3*(400-122.8) = 205.96 继续向 400 收敛
    # → D_kv = 10*205.96*700 = 1441720 → N_d_kv = ceil(1441720/90000) = 17（增大）
    p.update(
        PlannerSnapshot(
            dt=1.0,
            prefill=_role(req=10, tokens=5000, prefill_time_sum=1.0, instances=1),
            decode=_role(
                req=10,
                tokens=2000,
                tps=[2000.0],
                instances=1,
                queue_time_sum=0.0,
                queue_time_count=30,
                decode_time_sum=8040.0,
                decode_time_count=30,
                kv_tokens_per_instance_decode=100000,
            ),
        )
    )
    out = p.compute()
    assert out["decode_replicas_required"] == 17.0


def test_kv_missing_falls_back_to_throughput():
    p = CapacityPlanner(PlannerConfig())
    p.update(
        PlannerSnapshot(
            dt=1.0,
            prefill=None,
            decode=_role(req=0, instances=1, tps=[0.0], kv_tokens_per_instance_decode=0.0),
        )
    )
    p.update(
        PlannerSnapshot(
            dt=1.0,
            prefill=None,
            decode=_role(
                req=10,
                tokens=2000,
                tps=[2000.0],
                instances=1,
                queue_time_sum=0.0,
                queue_time_count=10,
                decode_time_sum=4000.0,
                decode_time_count=10,
                kv_tokens_per_instance_decode=0.0,
            ),
        )
    )
    out = p.compute()
    assert out["decode_replicas_required"] == 2.0
    assert "kv_demand_tokens" not in out


def test_utilization_value_and_skip_when_uncalibrated():
    out = _loaded_planner().compute()
    # U_p = 5000/(1*5000) = 1.0；U_d = 2000/(1*2000) = 1.0
    assert abs(out["prefill_utilization"] - 1.0) < 1e-6
    fresh = CapacityPlanner(PlannerConfig()).compute()
    assert "prefill_utilization" not in fresh  # 无数据不输出


def test_planner_config_rejects_invalid_values():
    """PlannerConfig __post_init__ 校验：非法取值带参数名抛 ValueError。"""
    PlannerConfig()  # defaults are valid
    with pytest.raises(ValueError, match="target_utilization"):
        PlannerConfig(target_utilization=0.0)
    with pytest.raises(ValueError, match="target_utilization"):
        PlannerConfig(target_utilization=1.5)
    with pytest.raises(ValueError, match="kv_target_utilization"):
        PlannerConfig(kv_target_utilization=0.0)
    with pytest.raises(ValueError, match="kv_target_utilization"):
        PlannerConfig(kv_target_utilization=1.1)
    with pytest.raises(ValueError, match="ema_alpha"):
        PlannerConfig(ema_alpha=0.0)
    with pytest.raises(ValueError, match="ema_alpha"):
        PlannerConfig(ema_alpha=1.5)
    with pytest.raises(ValueError, match="capacity_decay"):
        PlannerConfig(capacity_decay=-0.1)
    with pytest.raises(ValueError, match="capacity_decay"):
        PlannerConfig(capacity_decay=1.0)
    with pytest.raises(ValueError, match="prefill_tps_capacity_prior"):
        PlannerConfig(prefill_tps_capacity_prior=-1.0)
    with pytest.raises(ValueError, match="decode_tps_capacity_prior"):
        PlannerConfig(decode_tps_capacity_prior=-1.0)
    with pytest.raises(ValueError, match="capacity_window_cycles"):
        PlannerConfig(capacity_window_cycles=0)


def test_decode_all_zero_tps_does_not_calibrate():
    """decode 分支：全 0 TPS 列表不算有效标定；正样本出现后才置 calibrated。"""
    p = CapacityPlanner(PlannerConfig())  # no prior
    p.update(
        PlannerSnapshot(
            dt=3.0,
            prefill=None,
            decode=_role(req=100, tokens=1000, tps=[0.0, 0.0]),
        )
    )
    out = p.compute()
    assert out["capacity_calibrated_decode"] == 0.0
    p.update(
        PlannerSnapshot(
            dt=3.0,
            prefill=None,
            decode=_role(req=110, tokens=2000, tps=[0.0, 50.0]),
        )
    )
    out = p.compute()
    assert out["capacity_calibrated_decode"] == 1.0


def test_kv_capacity_sticky_when_info_missing():
    """K 是静态量：某周期所有 decode 实例未暴露 cache_config_info 时不得回落为 0。"""
    p = CapacityPlanner(PlannerConfig())

    def decode_snap(req, tokens, k, decode_sum, count):
        return PlannerSnapshot(
            dt=3.0,
            prefill=None,
            decode=_role(
                req=req,
                tokens=tokens,
                instances=1,
                tps=[100.0],
                queue_time_sum=0.0,
                queue_time_count=count,
                decode_time_sum=decode_sum,
                decode_time_count=count,
                kv_tokens_per_instance_decode=k,
            ),
        )

    # 周期 0：基线（计数器 delta 需两次 update 才产样本）
    p.update(decode_snap(0.0, 0.0, 100.0, 0.0, 0.0))
    # 周期 1：K=100 使 KV 约束 binding
    # （λ=1/3、L̄=200、W_kv=4 → D_kv≈266.7 → N_d_kv=ceil(266.7/90)=3 > N_d_tp=1）
    p.update(decode_snap(1.0, 200.0, 100.0, 4.0, 1.0))
    out_with_k = p.compute()
    assert "kv_demand_tokens" in out_with_k
    assert out_with_k["decode_replicas_required"] == 3.0

    # 周期 2：cache_config_info 缺失（K=0），KV 约束不得闪断
    p.update(decode_snap(2.0, 400.0, 0.0, 8.0, 2.0))
    out_missing_k = p.compute()
    assert "kv_demand_tokens" in out_missing_k
    assert out_missing_k["decode_replicas_required"] == 3.0

    # 周期 3：解析到新的有效 K（200），正常更新（N_d_kv=ceil(266.7/180)=2）
    p.update(decode_snap(3.0, 600.0, 200.0, 12.0, 3.0))
    assert p.compute()["decode_replicas_required"] == 2.0
