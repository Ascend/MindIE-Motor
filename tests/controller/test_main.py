# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import MagicMock, patch

from motor.controller.controller import Controller, _OBSERVER_NAMES


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


def test_main_no_config_flag():
    """When --config is not given, from_json(None) falls back to env vars."""
    with (
        patch("motor.controller.main.ControllerConfig.from_json") as mock_fj,
        patch("motor.controller.main.reconfigure_logging"),
        patch("motor.controller.main.run_port_setup_or_exit"),
        patch("motor.controller.main.Controller.run", return_value=0),
        patch("sys.argv", ["main.py"]),
    ):
        from motor.controller.main import main

        main()

        mock_fj.assert_called_once_with(None)


def test_main_with_config_short_flag():
    with (
        patch("motor.controller.main.ControllerConfig.from_json") as mock_fj,
        patch("motor.controller.main.reconfigure_logging"),
        patch("motor.controller.main.run_port_setup_or_exit"),
        patch("motor.controller.main.Controller.run", return_value=0),
        patch("sys.argv", ["main.py", "-c", "/tmp/cfg.json"]),
    ):
        from motor.controller.main import main

        main()

        mock_fj.assert_called_once_with("/tmp/cfg.json")


def test_main_with_config_long_flag():
    with (
        patch("motor.controller.main.ControllerConfig.from_json") as mock_fj,
        patch("motor.controller.main.reconfigure_logging"),
        patch("motor.controller.main.run_port_setup_or_exit"),
        patch("motor.controller.main.Controller.run", return_value=0),
        patch("sys.argv", ["main.py", "--config", "/tmp/cfg.json"]),
    ):
        from motor.controller.main import main

        main()

        mock_fj.assert_called_once_with("/tmp/cfg.json")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides):
    """Create a mock ControllerConfig with sensible defaults."""
    cfg = MagicMock()
    cfg.fault_tolerance_config.enable_fault_tolerance = False
    cfg.observability_config.observability_enable = False
    cfg.standby_config.enable_master_standby = False
    cfg.logging_config = MagicMock()
    for k, v in overrides.items():
        if "." not in k:
            setattr(cfg, k, v)
        else:
            _set_nested(cfg, k, v)
    return cfg


def _set_nested(cfg, dotted, value):
    parts = dotted.split(".")
    obj = cfg
    for p in parts[:-1]:
        obj = getattr(obj, p)
    setattr(obj, parts[-1], value)


# ---------------------------------------------------------------------------
# init_modules
# ---------------------------------------------------------------------------


def test_init_modules_standalone():
    cfg = _make_config()
    with (
        patch("motor.controller.controller.InstanceAssembler") as mock_asm,
        patch("motor.controller.controller.EventPusher") as mock_pusher,
        patch("motor.controller.controller.InstanceManager") as mock_mgr,
        patch("motor.controller.controller.ControllerAPI") as mock_api,
    ):
        mock_asm.return_value = MagicMock(name="InstanceAssembler")
        mock_pusher.return_value = MagicMock(name="EventPusher")
        mock_mgr.return_value = MagicMock(name="InstanceManager")
        mock_api.return_value = MagicMock(name="ControllerAPI")

        ctrl = Controller(cfg)
        ctrl.init_modules()

        assert "InstanceAssembler" in ctrl.modules
        assert "EventPusher" in ctrl.modules
        assert "InstanceManager" in ctrl.modules
        assert "ControllerAPI" in ctrl.modules
        assert "FaultManager" not in ctrl.modules
        assert "Observability" not in ctrl.modules

        # Observer should be attached
        ctrl.modules["InstanceManager"].attach.assert_called_once_with(ctrl.modules["EventPusher"])


def test_init_modules_no_instance_manager():
    cfg = _make_config()
    with (
        patch("motor.controller.controller.InstanceAssembler"),
        patch("motor.controller.controller.EventPusher"),
        patch("motor.controller.controller.InstanceManager", return_value=None),
        patch("motor.controller.controller.ControllerAPI"),
        patch("motor.controller.controller.logger") as mock_log,
    ):
        ctrl = Controller(cfg)
        ctrl.init_modules()

        mock_log.error.assert_called_once_with("InstanceManager not found in modules")


def test_init_modules_with_fault_tolerance():
    cfg = _make_config(**{"fault_tolerance_config.enable_fault_tolerance": True})
    mock_fm = MagicMock(name="FaultManager")

    with (
        patch("motor.controller.controller.InstanceAssembler"),
        patch("motor.controller.controller.EventPusher"),
        patch("motor.controller.controller.InstanceManager") as mock_mgr,
        patch("motor.controller.controller.ControllerAPI"),
        patch(
            "motor.controller.fault_tolerance.FaultManager",
            return_value=mock_fm,
        ),
    ):
        mock_mgr.return_value = MagicMock(name="InstanceManager")

        ctrl = Controller(cfg)
        ctrl.init_modules()

        assert "FaultManager" in ctrl.modules
        # Both EventPusher and FaultManager should be attached (both in _OBSERVER_NAMES)
        assert ctrl.modules["InstanceManager"].attach.call_count == 2


def test_init_modules_with_observability():
    cfg = _make_config(**{"observability_config.observability_enable": True})

    with (
        patch("motor.controller.controller.InstanceAssembler"),
        patch("motor.controller.controller.EventPusher"),
        patch("motor.controller.controller.InstanceManager") as mock_mgr,
        patch("motor.controller.controller.ControllerAPI"),
        patch(
            "motor.controller.observability.observability.Observability",
        ) as mock_obs,
    ):
        mock_mgr.return_value = MagicMock(name="InstanceManager")
        mock_obs.return_value = MagicMock(name="Observability")

        ctrl = Controller(cfg)
        ctrl.init_modules()

        assert "Observability" in ctrl.modules


# ---------------------------------------------------------------------------
# _start_modules
# ---------------------------------------------------------------------------


def test_start_modules_standalone():
    cfg = _make_config()

    with (
        patch("motor.controller.controller.InstanceAssembler"),
        patch("motor.controller.controller.EventPusher"),
        patch("motor.controller.controller.InstanceManager"),
        patch("motor.controller.controller.ControllerAPI"),
        patch("motor.controller.controller.Controller._start_config_watcher"),
    ):
        ctrl = Controller(cfg)
        ctrl.init_modules()

        # Give each module a mock start()
        for m in ctrl.modules.values():
            m.start = MagicMock()

        ctrl._start_modules()

        # All modules should be started
        for m in ctrl.modules.values():
            m.start.assert_called_once()


def test_start_modules_standby_mode():
    cfg = _make_config(**{"standby_config.enable_master_standby": True})

    with (
        patch("motor.controller.controller.InstanceAssembler"),
        patch("motor.controller.controller.EventPusher"),
        patch("motor.controller.controller.InstanceManager"),
        patch("motor.controller.controller.ControllerAPI"),
        patch("motor.controller.controller.Controller._start_config_watcher"),
        patch("motor.controller.controller.StandbyManager") as mock_sm,
    ):
        mock_sm.return_value = MagicMock(name="StandbyManager")

        ctrl = Controller(cfg)
        ctrl.init_modules()

        # Give each module a mock start()
        for m in ctrl.modules.values():
            m.start = MagicMock()

        ctrl._start_modules()

        # Only ControllerAPI should be started in standby mode
        ctrl.modules["ControllerAPI"].start.assert_called_once()
        ctrl.modules["InstanceManager"].start.assert_not_called()
        ctrl.modules["InstanceAssembler"].start.assert_not_called()
        ctrl.modules["EventPusher"].start.assert_not_called()

        # StandbyManager should be created and started
        mock_sm.assert_called_once_with(cfg)
        mock_sm.return_value.start.assert_called_once()


def test_start_modules_standby_mode_with_fault_tolerance():
    """FaultManager is also excluded in standby startup."""
    cfg = _make_config(
        **{
            "standby_config.enable_master_standby": True,
            "fault_tolerance_config.enable_fault_tolerance": True,
        }
    )

    with (
        patch("motor.controller.controller.InstanceAssembler"),
        patch("motor.controller.controller.EventPusher"),
        patch("motor.controller.controller.InstanceManager"),
        patch("motor.controller.controller.ControllerAPI"),
        patch(
            "motor.controller.fault_tolerance.FaultManager",
            return_value=MagicMock(name="FaultManager"),
        ),
        patch("motor.controller.controller.Controller._start_config_watcher"),
        patch("motor.controller.controller.StandbyManager") as mock_sm,
    ):
        mock_sm.return_value = MagicMock(name="StandbyManager")

        ctrl = Controller(cfg)
        ctrl.init_modules()

        for m in ctrl.modules.values():
            m.start = MagicMock()

        ctrl._start_modules()

        ctrl.modules["ControllerAPI"].start.assert_called_once()
        ctrl.modules["FaultManager"].start.assert_not_called()


# ---------------------------------------------------------------------------
# _on_become_master / _on_become_standby
# ---------------------------------------------------------------------------


def test_on_become_master_modules_not_initialized():
    cfg = _make_config()
    ctrl = Controller(cfg)
    assert len(ctrl.modules) == 0

    with (
        patch.object(ctrl, "init_modules") as mock_init,
        patch("motor.controller.controller.logger"),
    ):
        ctrl._on_become_master(should_report_event=False)

        mock_init.assert_called_once()


def test_on_become_master_modules_already_initialized():
    cfg = _make_config()
    ctrl = Controller(cfg)
    ctrl.modules["ControllerAPI"] = MagicMock(name="ControllerAPI")
    ctrl.modules["FakeModule"] = MagicMock(name="FakeModule")
    ctrl.modules["FakeModule"].start = MagicMock()

    with (
        patch.object(ctrl, "init_modules") as mock_init,
        patch("motor.controller.controller.logger"),
    ):
        ctrl._on_become_master(should_report_event=False)

        mock_init.assert_not_called()
        ctrl.modules["FakeModule"].start.assert_called_once()
        # ControllerAPI should NOT be started again
        ctrl.modules["ControllerAPI"].start.assert_not_called()


def test_on_become_master_with_report_event():
    cfg = _make_config()
    ctrl = Controller(cfg)
    ctrl.modules["FakeModule"] = MagicMock(name="FakeModule")
    ctrl.modules["FakeModule"].start = MagicMock()

    with (
        patch("motor.common.alarm.master_to_slave_event.MasterToSlaveEvent") as mock_event_cls,
        patch("motor.controller.observability.observability.Observability") as mock_obs,
        patch("motor.controller.controller.logger"),
    ):
        mock_obs.return_value = MagicMock(name="Observability")

        ctrl._on_become_master(should_report_event=True)

        mock_event_cls.assert_called_once()
        mock_obs.return_value.add_alarm.assert_called_once()


def test_on_become_standby():
    cfg = _make_config()
    ctrl = Controller(cfg)

    mock_stop = MagicMock()
    mock_stop.is_alive.return_value = True
    ctrl.modules["ControllerAPI"] = MagicMock(name="ControllerAPI")
    ctrl.modules["FakeModule"] = mock_stop

    with patch("motor.controller.controller.logger"):
        ctrl._on_become_standby()

        # ControllerAPI should NOT be stopped
        ctrl.modules["ControllerAPI"].stop.assert_not_called()
        # Other modules should be stopped
        mock_stop.stop.assert_called_once()


def test_on_become_standby_module_not_alive():
    """Modules that are not alive should be skipped."""
    cfg = _make_config()
    ctrl = Controller(cfg)

    mock_stop = MagicMock()
    mock_stop.is_alive.return_value = False
    ctrl.modules["ControllerAPI"] = MagicMock(name="ControllerAPI")
    ctrl.modules["FakeModule"] = mock_stop

    with patch("motor.controller.controller.logger"):
        ctrl._on_become_standby()

        mock_stop.stop.assert_not_called()


def test_on_become_standby_module_no_is_alive():
    """Modules with stop() but without is_alive() (e.g. Observability) should still be stopped."""
    cfg = _make_config()
    ctrl = Controller(cfg)

    # Use spec to create a mock that has stop() but NOT is_alive()
    mock_stop = MagicMock(spec=["stop"])
    ctrl.modules["ControllerAPI"] = MagicMock(name="ControllerAPI")
    ctrl.modules["Observability"] = mock_stop

    with patch("motor.controller.controller.logger"):
        ctrl._on_become_standby()

        mock_stop.stop.assert_called_once()


# ---------------------------------------------------------------------------
# on_config_updated
# ---------------------------------------------------------------------------


def test_on_config_updated_no_change():
    cfg = _make_config(**{"fault_tolerance_config.enable_fault_tolerance": False})
    ctrl = Controller(cfg)

    m1 = MagicMock()
    m2 = MagicMock()
    ctrl.modules["Module1"] = m1
    ctrl.modules["Module2"] = m2

    with patch("motor.controller.controller.logger"):
        ctrl.on_config_updated()

    m1.update_config.assert_called_once_with(cfg)
    m2.update_config.assert_called_once_with(cfg)


def test_on_config_updated_enable_fault_tolerance():
    cfg = _make_config(**{"fault_tolerance_config.enable_fault_tolerance": True})
    ctrl = Controller(cfg)
    ctrl._previous_fault_tolerance_enabled = False

    mock_fm = MagicMock(name="FaultManager")
    mock_im = MagicMock(name="InstanceManager")
    mock_im.get_active_instances.return_value = []
    mock_im.get_inactive_instances.return_value = []
    ctrl.modules["InstanceManager"] = mock_im

    with (
        patch("motor.controller.controller.logger"),
        patch(
            "motor.controller.fault_tolerance.FaultManager",
            return_value=mock_fm,
        ),
    ):
        ctrl.on_config_updated()

    assert ctrl.modules["FaultManager"] is mock_fm
    mock_fm.start.assert_called_once()
    mock_im.attach.assert_called_once_with(mock_fm)
    assert ctrl._previous_fault_tolerance_enabled is True


def test_on_config_updated_disable_fault_tolerance():
    cfg = _make_config()
    ctrl = Controller(cfg)
    ctrl._previous_fault_tolerance_enabled = True

    mock_fm = MagicMock(name="FaultManager")
    ctrl.modules["FaultManager"] = mock_fm

    with patch("motor.controller.controller.logger"):
        ctrl.on_config_updated()

    mock_fm.stop.assert_called_once()
    assert "FaultManager" not in ctrl.modules
    assert ctrl._previous_fault_tolerance_enabled is False


def test_on_config_updated_enable_fault_tolerance_exception():
    cfg = _make_config(**{"fault_tolerance_config.enable_fault_tolerance": True})
    ctrl = Controller(cfg)
    ctrl._previous_fault_tolerance_enabled = False

    with (
        patch("motor.controller.controller.logger") as mock_log,
        patch(
            "motor.controller.fault_tolerance.FaultManager",
            side_effect=Exception("Test error"),
        ),
    ):
        ctrl.on_config_updated()

        mock_log.exception.assert_called_with("Failed to start FaultManager")


def test_on_config_updated_disable_fault_tolerance_exception():
    cfg = _make_config()
    ctrl = Controller(cfg)
    ctrl._previous_fault_tolerance_enabled = True

    mock_fm = MagicMock(name="FaultManager")
    mock_fm.stop.side_effect = Exception("Test error")
    ctrl.modules["FaultManager"] = mock_fm

    with (
        patch("motor.controller.controller.logger") as mock_log,
    ):
        ctrl.on_config_updated()

        mock_log.exception.assert_called_with("Failed to stop FaultManager")


def test_on_config_updated_module_update_exception():
    cfg = _make_config()
    ctrl = Controller(cfg)

    m1 = MagicMock()
    m2 = MagicMock()
    m2.update_config.side_effect = Exception("Update error")
    ctrl.modules["Module1"] = m1
    ctrl.modules["Module2"] = m2

    # The base class catches exceptions per-module and logs via exception()
    # pylint just needs to see the call happens
    with patch("motor.controller.controller.logger"):
        ctrl.on_config_updated()

    m1.update_config.assert_called_once_with(cfg)


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


def test_shutdown_stops_standby_manager():
    cfg = _make_config()
    ctrl = Controller(cfg)

    mock_sm = MagicMock(name="StandbyManager")
    ctrl._standby_manager = mock_sm

    with (
        patch("motor.controller.controller.logger"),
        patch.object(ctrl, "stop_all_modules") as mock_stop_all,
    ):
        ctrl.shutdown()

        mock_sm.stop.assert_called_once()
        mock_stop_all.assert_called_once()


def test_shutdown_no_standby_manager():
    cfg = _make_config()
    ctrl = Controller(cfg)

    with (
        patch("motor.controller.controller.logger"),
        patch.object(ctrl, "stop_all_modules") as mock_stop_all,
    ):
        ctrl.shutdown()

        mock_stop_all.assert_called_once()


# ---------------------------------------------------------------------------
# exit_code
# ---------------------------------------------------------------------------


def test_exit_code():
    ctrl = Controller(_make_config())
    assert ctrl.exit_code == 0


# ---------------------------------------------------------------------------
# _OBSERVER_NAMES
# ---------------------------------------------------------------------------


def test_observer_names():
    assert "EventPusher" in _OBSERVER_NAMES
    assert "FaultManager" in _OBSERVER_NAMES
