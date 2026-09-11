# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""PD 配比验收仿真台：确定性负载 case 驱动真实 CapacityPlanner，验收公式恒等（≤1 实例误差）。"""

import math

import pytest

from motor.coordinator.metrics.capacity_planner import (
    CapacityPlanner,
    PlannerConfig,
    PlannerSnapshot,
    RoleSnapshot,
)

DT = 3.0  # 固定采集周期（秒）：无 wall-clock 依赖
CONVERGENCE_CYCLES = 3  # α=0.85 时 3 周期残留 0.15^2 ≈ 2%，视为收敛
EMA_CYCLES = 30  # 长程运行轮数：验证收敛后不再漂出 ±1


class LoadCase:
    """确定性负载 case：请求形态（L̄_in/L̄_out/λ）+ 单实例真实容量（C_p/C_d/K）+ KV 驻留 W_kv。"""

    def __init__(self, name, input_len, output_len, rps, c_p, c_d, k_tokens, w_kv):
        self.name = name
        self.input_len = input_len  # 每请求输入 token 数
        self.output_len = output_len  # 每请求输出 token 数
        self.rps = rps  # 请求到达率 λ
        self.c_p = c_p  # 单实例 prefill 真实容量（tokens/s）
        self.c_d = c_d  # 单实例 decode 真实容量（tokens/s）
        self.k_tokens = k_tokens  # 单实例 KV token 容量
        self.w_kv = w_kv  # 每请求 KV 驻留时长（queue+decode，秒）

    def at_rps(self, rps):
        """同构不同速率的 case（冷启动爬坡用）。"""
        return LoadCase(
            self.name,
            self.input_len,
            self.output_len,
            rps,
            self.c_p,
            self.c_d,
            self.k_tokens,
            self.w_kv,
        )

    def theoretical(self, rho=0.8, rho_kv=0.9):
        """理论所需实例数 (N_p, N_d, N_d_tp, N_d_kv)：N_d = max(吞吐约束, KV 约束)。"""
        n_p = math.ceil(self.rps * self.input_len / (self.c_p * rho))
        n_d_tp = math.ceil(self.rps * self.output_len / (self.c_d * rho))
        n_d_kv = math.ceil(self.rps * self.w_kv * (self.input_len + self.output_len) / (self.k_tokens * rho_kv))
        return n_p, max(n_d_tp, n_d_kv), n_d_tp, n_d_kv

    def role_snapshots(self, n_p, n_d, cycles):
        """生成 cycles 个连续 PlannerSnapshot（累计计数器跨周期连续推进）。"""
        simulator = LoadSimulator()
        return [simulator.step(self, n_p, n_d) for _ in range(cycles)]


class LoadSimulator:
    """按 case 参数合成引擎侧累计计数器：确定性排队模型，无随机、无真实时间。"""

    QUEUE_FRACTION = 0.1  # W_kv 拆分：queue 占 10%，decode 占 90%

    def __init__(self):
        self._req_total = 0.0
        self._prompt_tokens = 0.0
        self._gen_tokens = 0.0
        self._prefill_time = 0.0
        self._queue_time = 0.0
        self._decode_time = 0.0
        self._time_count = 0.0  # queue/decode 样本数计数器（两者同步推进）

    def step(self, case, n_p, n_d):
        """推进一个采集周期并返回周期末快照（计数器为角色级累计语义）。"""
        completed = case.rps * DT
        self._req_total += completed
        self._prompt_tokens += completed * case.input_len
        self._gen_tokens += completed * case.output_len
        self._prefill_time += completed * case.input_len / case.c_p
        self._queue_time += completed * case.w_kv * self.QUEUE_FRACTION
        self._decode_time += completed * case.w_kv * (1.0 - self.QUEUE_FRACTION)
        self._time_count += completed
        prefill = RoleSnapshot(
            active_instances=n_p,
            instance_tps=[min(case.rps * case.input_len, n_p * case.c_p) / n_p] * n_p,
            request_success_total=self._req_total,
            tokens_total=self._prompt_tokens,
            prefill_time_sum=self._prefill_time,
        )
        decode = RoleSnapshot(
            active_instances=n_d,
            instance_tps=[min(case.rps * case.output_len, n_d * case.c_d) / n_d] * n_d,
            request_success_total=self._req_total,
            tokens_total=self._gen_tokens,
            queue_time_sum=self._queue_time,
            queue_time_count=self._time_count,
            decode_time_sum=self._decode_time,
            decode_time_count=self._time_count,
            kv_tokens_per_instance_decode=case.k_tokens,
        )
        return PlannerSnapshot(dt=DT, prefill=prefill, decode=decode)


# case 矩阵（同 λ=3.7）：prefill 重 / decode 重吞吐 binding / KV 约束 binding / 均衡
PREFILL_HEAVY = LoadCase("prefill_heavy", 4000, 500, 3.7, 2000.0, 2000.0, 2_000_000, 2.0)
DECODE_HEAVY = LoadCase("decode_heavy", 500, 4000, 3.7, 2000.0, 2000.0, 2_000_000, 2.0)
KV_BOUND = LoadCase("kv_bound", 500, 4000, 3.7, 2000.0, 2000.0, 20_000, 30.0)
BALANCED = LoadCase("balanced", 1000, 1000, 3.7, 2000.0, 2000.0, 2_000_000, 1.0)
# 结构切换目标：λ 与输入不变、输出变长（decode 重吞吐 binding）
SWITCH_DECODE_HEAVY = LoadCase("switch_decode_heavy", 1000, 4000, 3.7, 2000.0, 2000.0, 2_000_000, 4.0)
CASE_MATRIX = [PREFILL_HEAVY, DECODE_HEAVY, KV_BOUND, BALANCED]


def _calibrated_config(case):
    """先验 = 真实容量：跳过冷启动，直接验收公式恒等。"""
    return PlannerConfig(prefill_tps_capacity_prior=case.c_p, decode_tps_capacity_prior=case.c_d)


def _run_case(case, n_p=1, n_d=1, cycles=EMA_CYCLES):
    """过载运行点（默认 1+1）驱动 planner：实例饱和使每实例 TPS 恰好暴露真实容量 C。"""
    planner = CapacityPlanner(_calibrated_config(case))
    outputs = []
    for snapshot in case.role_snapshots(n_p, n_d, cycles):
        planner.update(snapshot)
        outputs.append(planner.compute())
    return outputs


def _required(out):
    return out["prefill_replicas_required"], out["decode_replicas_required"]


def _within_one(req, theory):
    return abs(req[0] - theory[0]) <= 1 and abs(req[1] - theory[1]) <= 1


def test_case_matrix_covers_required_shapes():
    n_p, n_d, _, _ = PREFILL_HEAVY.theoretical()
    assert n_p > n_d  # prefill 明显重于 decode（10 vs 2）
    n_p, n_d, n_d_tp, n_d_kv = DECODE_HEAVY.theoretical()
    assert n_d == n_d_tp > n_d_kv and n_d > n_p  # 吞吐约束 binding（10 vs 2）
    _, n_d, n_d_tp, n_d_kv = KV_BOUND.theoretical()
    assert n_d == n_d_kv and n_d_kv >= 2 * n_d_tp  # KV 约束 binding，至少 2 倍（28 vs 10）
    n_p, n_d, _, _ = BALANCED.theoretical()
    assert n_p == n_d  # 均衡（3 vs 3）


@pytest.mark.parametrize("case", CASE_MATRIX, ids=lambda c: c.name)
def test_formula_identity_after_ema_convergence(case):
    """公式恒等：第 3 采集周期起（含），每周期 required 与 case 理论推导值误差 ≤1 实例。"""
    n_p_theory, n_d_theory, _, _ = case.theoretical()
    outputs = _run_case(case)
    # α=0.85 下确定性负载第 2 周期即取到首个精确样本，第 3 周期起应精确等于
    for out in outputs[CONVERGENCE_CYCLES - 1 :]:
        n_p_req, n_d_req = _required(out)
        assert abs(n_p_req - n_p_theory) <= 1
        assert abs(n_d_req - n_d_theory) <= 1


def test_kv_bound_required_equals_kv_constraint():
    """KV binding case：decode required 恒等于 KV 约束理论值，而非吞吐约束值。"""
    _, _, n_d_tp, n_d_kv = KV_BOUND.theoretical()
    for out in _run_case(KV_BOUND)[-5:]:  # EMA 完全收敛后，最后 5 周期恒等
        n_d_req = out["decode_replicas_required"]
        assert n_d_req == n_d_kv
        assert n_d_req > n_d_tp


def test_closed_loop_fixed_point():
    """闭环：应用 required 作为下一轮实例数，收敛后保持 5 轮不再变化。"""
    planner = CapacityPlanner(_calibrated_config(KV_BOUND))
    simulator = LoadSimulator()
    n_p = n_d = 1  # 首轮 (1,1)
    history = []
    for _ in range(10):
        planner.update(simulator.step(KV_BOUND, n_p, n_d))
        req = _required(planner.compute())
        history.append(req)
        n_p, n_d = max(1, int(req[0])), max(1, int(req[1]))
    converged = next(i for i in range(1, len(history)) if _within_one(history[i], history[i - 1]))
    stable = history[converged:]
    assert len(stable) >= 5
    assert all(req == stable[0] for req in stable)
    n_p_theory, n_d_theory, _, _ = KV_BOUND.theoretical()
    assert _within_one(stable[0], (n_p_theory, n_d_theory))


def test_structural_switch_reconverges():
    """结构切换：均衡闭环收敛后切成 decode 重（λ 不变、输出变长），3 周期内重新收敛。"""
    planner = CapacityPlanner(_calibrated_config(BALANCED))
    simulator = LoadSimulator()
    n_p = n_d = 1
    for _ in range(8):  # 相位 1：均衡闭环收敛
        planner.update(simulator.step(BALANCED, n_p, n_d))
        base = _required(planner.compute())
        n_p, n_d = max(1, int(base[0])), max(1, int(base[1]))
    assert _within_one(base, BALANCED.theoretical()[:2])

    records = []
    for _ in range(CONVERGENCE_CYCLES):  # 相位 2：decode 重闭环
        planner.update(simulator.step(SWITCH_DECODE_HEAVY, n_p, n_d))
        req = _required(planner.compute())
        records.append(req)
        n_p, n_d = max(1, int(req[0])), max(1, int(req[1]))
    theory = SWITCH_DECODE_HEAVY.theoretical()[:2]
    within = [_within_one(req, theory) for req in records]
    first = within.index(True)  # 限定轮数（3 个采集周期）内达到 ≤1 误差
    assert all(within[first:])  # 收敛后至第 3 周期不再漂出 ±1
    delta_p = records[-1][0] - base[0]
    delta_d = records[-1][1] - base[1]
    assert delta_d > delta_p  # decode 侧增幅大于 prefill 侧


def test_cold_start_calibration():
    """冷启动：零先验起步、λ 三段爬坡，calibrated 翻转、容量单调学习、最终 ≤1 误差。"""
    planner = CapacityPlanner(PlannerConfig())  # 零先验
    simulator = LoadSimulator()
    initial = planner.compute()
    assert "capacity_calibrated_prefill" not in initial  # 无先验无观测
    assert "capacity_calibrated_decode" not in initial

    low = BALANCED.at_rps(BALANCED.rps * 0.15)
    mid = BALANCED.at_rps(BALANCED.rps * 0.4)
    planner.update(simulator.step(low, 1, 1))
    out = planner.compute()
    assert out["capacity_calibrated_prefill"] == 0.0  # 无 delta，prefill 尚未标定
    assert out["capacity_calibrated_decode"] == 1.0  # instance_tps 首观测即标定
    planner.update(simulator.step(low, 1, 1))
    out = planner.compute()
    assert out["capacity_calibrated_prefill"] == 1.0  # 首个 Δtokens/Δprefill_time 样本 → 翻转
    assert abs(out["prefill_capacity_tps"] - BALANCED.c_p) < 1e-6

    capacities = []  # λ 低 → 中 → 目标三段爬坡：C_d max 学习，段末容量单调不降
    for case in (low, mid, BALANCED):
        for _ in range(CONVERGENCE_CYCLES):  # α=0.85：每段 3 周期内收敛
            planner.update(simulator.step(case, 1, 1))
        out = planner.compute()
        capacities.append(out["decode_capacity_tps"])
        # 爬坡段末尾即验收：required 与该段理论值误差 ≤1
        # （低/中段未饱和，C_d 只学到观测水位，decode 侧最多差 1 实例，仍在容差内）
        assert _within_one(_required(out), case.theoretical()[:2])
    assert capacities[0] <= capacities[1] <= capacities[2]
    assert abs(capacities[2] - BALANCED.c_d) / BALANCED.c_d < 0.01  # 目标段饱和 → 学到真实 C_d

    for _ in range(CONVERGENCE_CYCLES):  # 目标水位跑 3 周期验证收敛后不漂出
        planner.update(simulator.step(BALANCED, 1, 1))
        out = planner.compute()
    assert _within_one(_required(out), BALANCED.theoretical()[:2])


@pytest.mark.parametrize("case", CASE_MATRIX, ids=lambda c: c.name)
def test_deterministic_repeated_runs(case):
    """可重复：同 case 连跑两次，每周期 compute() 输出序列完全一致。"""
    assert _run_case(case) == _run_case(case)
