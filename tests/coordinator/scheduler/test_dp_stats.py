# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for coordinator per-DP statistics logger."""

import unittest
from unittest.mock import patch

from motor.coordinator.scheduler.runtime.dp_stats import DpStatsLogger

_IDX = {
    "instance": 1,
    "dp_rank": 2,
    "requests": 3,
    "active_tokens": 4,
}


def _values(mock_log, index: int = -1) -> tuple:
    return mock_log.call_args_list[index].args


class TestDpStatsLogger(unittest.TestCase):
    def test_record_does_not_emit(self):
        logger = DpStatsLogger(window_sec=10)
        with patch("motor.coordinator.scheduler.runtime.dp_stats.logger.info") as mock_log:
            logger.record(instance_id=1, dp_rank=0)
            logger.record(instance_id=1, dp_rank=0)
            logger.record(instance_id=1, dp_rank=1)
            mock_log.assert_not_called()
            assert logger._counter[("1", "0")] == 2
            assert logger._counter[("1", "1")] == 1

    def test_emit_window_joins_requests_and_tokens(self):
        logger = DpStatsLogger(window_sec=60)
        with patch("motor.coordinator.scheduler.runtime.dp_stats.logger.info") as mock_log:
            logger.record(instance_id=1, dp_rank=0)
            logger.record(instance_id=1, dp_rank=0)
            logger.record(instance_id=2, dp_rank=1)
            logger.emit_window([(1, 0, 5.5), (1, 10, 0.0)])

            assert mock_log.call_count == 2
            by_key = {}
            for i in range(2):
                values = _values(mock_log, i)
                by_key[(values[_IDX["instance"]], values[_IDX["dp_rank"]])] = values
            fmt = by_key[("1", "0")][0]
            assert fmt == "dp_stats instance=%s dp_rank=%s requests=%d active_tokens=%s"
            assert by_key[("1", "0")][_IDX["requests"]] == 2
            assert by_key[("1", "0")][_IDX["active_tokens"]] == 5.5
            assert ("1", "10") not in by_key
            assert by_key[("2", "1")][_IDX["requests"]] == 1
            assert by_key[("2", "1")][_IDX["active_tokens"]] == 0.0

    def test_emit_window_clears_counter_for_next_tick(self):
        logger = DpStatsLogger(window_sec=60)
        with patch("motor.coordinator.scheduler.runtime.dp_stats.logger.info") as mock_log:
            logger.record(instance_id=1, dp_rank=0)
            logger.emit_window([(1, 0, 3.0)])
            logger.emit_window([(1, 0, 3.0)])
            assert mock_log.call_count == 2
            assert _values(mock_log, 0)[_IDX["requests"]] == 1
            assert _values(mock_log, 1)[_IDX["requests"]] == 0
            assert _values(mock_log, 1)[_IDX["active_tokens"]] == 3.0

    def test_float_window_sec_uses_integer_buckets(self):
        """JSON 30.5 must not produce float buckets that miss other windows."""
        logger = DpStatsLogger(window_sec=30.5)
        assert logger._window_sec == 30

    def test_disabled_window_emits_nothing(self):
        logger = DpStatsLogger(window_sec=0)
        with patch("motor.coordinator.scheduler.runtime.dp_stats.logger.info") as mock_log:
            logger.record(instance_id=1, dp_rank=0)
            logger.emit_window([(1, 0, 9.0)])
            mock_log.assert_not_called()

    def test_empty_window_is_silent(self):
        with patch("motor.coordinator.scheduler.runtime.dp_stats.logger.info") as mock_log:
            DpStatsLogger(window_sec=60).emit_window([])
            mock_log.assert_not_called()

    def test_emit_window_skips_idle_dp(self):
        """Idle DPs (requests=0 and active_tokens=0) must not print."""
        logger = DpStatsLogger(window_sec=60)
        with patch("motor.coordinator.scheduler.runtime.dp_stats.logger.info") as mock_log:
            logger.emit_window([(1, 0, 0.0), (1, 1, 0)])
            mock_log.assert_not_called()

            logger.record(instance_id=1, dp_rank=2)
            logger.emit_window([(1, 0, 0.0), (1, 2, 0.0)])
            assert mock_log.call_count == 1
            values = _values(mock_log)
            assert values[_IDX["instance"]] == "1"
            assert values[_IDX["dp_rank"]] == "2"
            assert values[_IDX["requests"]] == 1
            assert values[_IDX["active_tokens"]] == 0.0


if __name__ == "__main__":
    unittest.main()
