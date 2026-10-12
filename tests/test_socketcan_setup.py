from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from omnipicker_tool import socketcan_setup as setup
from omnipicker_tool.protocol_service import ProtocolServiceError


def make_link(*, up=False, fd=False, kind="can", bitrate=1_000_000, restart_ms=100):
    return {
        "ifname": "can0",
        "flags": ["NOARP", "UP"] if up else ["NOARP"],
        "operstate": "UNKNOWN" if up else "DOWN",
        "linkinfo": {
            "info_kind": kind,
            "info_data": {
                "ctrlmode": ["FD"] if fd else [],
                "state": "ERROR-ACTIVE" if up else "STOPPED",
                "bittiming": {"bitrate": bitrate, "sample_point": "0.750"},
                "data_bittiming": {"bitrate": 5_000_000, "sample_point": "0.750"},
                "restart_ms": restart_ms,
            },
        },
    }


class FakeIp:
    """An in-memory link, so no test may change the host's network."""

    def __init__(self, link, fail=None):
        self.link = deepcopy(link)
        self.calls = []
        self.fail = fail

    def __call__(self, command):
        self.calls.append(command)
        if self.fail:
            self.fail(command)
        if command[1] == "-json":
            return json.dumps([self.link])
        arguments = command[5:]
        data = self.link["linkinfo"]["info_data"]
        if arguments == ["up"]:
            self.link["flags"] = ["NOARP", "UP"]
            data["state"] = "ERROR-ACTIVE"
        elif arguments == ["down"]:
            self.link["flags"] = ["NOARP"]
            data["state"] = "STOPPED"
        else:
            assert arguments[:2] == ["type", "can"], arguments
            values = dict(zip(arguments[2::2], arguments[3::2]))
            if "bitrate" in values:
                data["bittiming"]["bitrate"] = int(values["bitrate"])
            if "sample-point" in values:
                data["bittiming"]["sample_point"] = str(min(float(values["sample-point"]), 0.75))
            if "dbitrate" in values:
                data["data_bittiming"]["bitrate"] = int(values["dbitrate"])
            if "dsample-point" in values:
                data["data_bittiming"]["sample_point"] = values["dsample-point"]
            if "fd" in values:
                data["ctrlmode"] = ["FD"] if values["fd"] == "on" else []
            if "restart-ms" in values:
                data["restart_ms"] = int(values["restart-ms"])
        return ""

    @property
    def writes(self):
        return [call[5:] for call in self.calls if call[1] == "link"]


class SocketCanSetupTests(unittest.TestCase):
    def run_setup(self, link, is_fd=False, fail=None):
        fake = FakeIp(link, fail)
        with patch.object(setup, "_executable", return_value="/usr/sbin/ip"), patch.object(
            setup, "_run", side_effect=fake
        ), patch.object(setup, "_authorize") as authorize:
            setup.ensure_socketcan_ready("can0", is_fd)
        authorize.assert_not_called()
        return fake

    def test_running_classic_with_quantized_sample_point_is_untouched(self):
        fake = self.run_setup(make_link(up=True))
        self.assertEqual(fake.writes, [])

    def test_running_fd_is_untouched(self):
        fake = self.run_setup(make_link(up=True, fd=True), is_fd=True)
        self.assertEqual(fake.writes, [])

    def test_requested_arbitration_sample_point_is_untouched(self):
        link = make_link(up=True)
        link["linkinfo"]["info_data"]["bittiming"]["sample_point"] = "0.800"
        self.assertEqual(self.run_setup(link).writes, [])

    def test_wrong_arbitration_sample_point_is_reconfigured(self):
        for sample_point in ("0.500", "0.875", None):
            with self.subTest(sample_point=sample_point):
                link = make_link(up=True)
                link["linkinfo"]["info_data"]["bittiming"]["sample_point"] = sample_point
                fake = self.run_setup(link)
                self.assertEqual(fake.writes[0], ["down"])
                self.assertTrue(setup._ready(fake.link, False))

    def test_quantized_fd_data_sample_point_is_untouched(self):
        link = make_link(up=True, fd=True)
        link["linkinfo"]["info_data"]["data_bittiming"]["sample_point"] = "0.700"
        self.assertEqual(self.run_setup(link, is_fd=True).writes, [])

    def test_wrong_fd_data_sample_point_is_reconfigured(self):
        for sample_point in ("0.500", "0.800", None):
            with self.subTest(sample_point=sample_point):
                link = make_link(up=True, fd=True)
                link["linkinfo"]["info_data"]["data_bittiming"]["sample_point"] = sample_point
                fake = self.run_setup(link, is_fd=True)
                self.assertEqual(fake.writes[0], ["down"])
                self.assertTrue(setup._ready(fake.link, True))

    def test_down_classic_gets_profile_without_fd_flag(self):
        fake = self.run_setup(make_link(restart_ms=0))
        self.assertEqual(fake.writes, [
            ["type", "can", "bitrate", "1000000", "sample-point", "0.8", "restart-ms", "100"],
            ["up"],
        ])

    def test_wrong_bitrate_is_changed_with_down_before_configuration(self):
        fake = self.run_setup(make_link(up=True, bitrate=500_000))
        self.assertEqual(fake.writes[0], ["down"])
        self.assertEqual(fake.writes[1][2:4], ["bitrate", "1000000"])
        self.assertEqual(fake.writes[2], ["up"])

    def test_fd_gets_arbitration_and_data_profiles(self):
        fake = self.run_setup(make_link(), is_fd=True)
        self.assertEqual(fake.writes[0], [
            "type", "can", "bitrate", "1000000", "sample-point", "0.8",
            "dbitrate", "5000000", "dsample-point", "0.75", "fd", "on", "restart-ms", "100",
        ])

    def test_switch_from_fd_explicitly_disables_fd(self):
        fake = self.run_setup(make_link(up=True, fd=True))
        arguments = fake.writes[1]
        self.assertEqual(arguments[arguments.index("fd") + 1], "off")

    def test_wrong_fd_data_bitrate_is_reconfigured(self):
        link = make_link(up=True, fd=True)
        link["linkinfo"]["info_data"]["data_bittiming"]["bitrate"] = 2_000_000
        fake = self.run_setup(link, is_fd=True)
        self.assertEqual(fake.writes[0], ["down"])
        self.assertTrue(setup._ready(fake.link, True))

    def test_bus_off_is_restarted(self):
        link = make_link(up=True)
        link["linkinfo"]["info_data"]["state"] = "BUS-OFF"
        fake = self.run_setup(link)
        self.assertEqual(fake.writes[0], ["down"])

    def test_vcan_only_brought_up(self):
        fake = self.run_setup(make_link(kind="vcan"))
        self.assertEqual(fake.writes, [["up"]])

    def test_running_vcan_is_untouched(self):
        fake = self.run_setup(make_link(up=True, kind="vcan"), is_fd=True)
        self.assertEqual(fake.writes, [])

    def test_other_network_device_rejected_without_writes(self):
        fake = FakeIp(make_link(kind="veth"))
        with patch.object(setup, "_executable", return_value="/usr/sbin/ip"), patch.object(
            setup, "_run", side_effect=fake
        ), self.assertRaisesRegex(ProtocolServiceError, "不是 CAN/vcan"):
            setup.ensure_socketcan_ready("can0", False)
        self.assertEqual(fake.writes, [])

    def test_invalid_name_never_invokes_command_or_authorization(self):
        for interface in ("", "--help", "can0;id", "../../etc", "can0\n", "x" * 16):
            with self.subTest(interface=interface), patch.object(setup, "_run") as run, patch.object(
                setup, "_authorize"
            ) as authorize, self.assertRaisesRegex(ProtocolServiceError, "接口名无效"):
                setup.ensure_socketcan_ready(interface, False)
            run.assert_not_called()
            authorize.assert_not_called()

    def test_permission_error_prompts_once(self):
        failure = setup._SetupError("Operation not permitted", permission_denied=True)
        with patch.object(setup, "_prepare", side_effect=failure), patch.object(setup, "_authorize") as authorize:
            setup.ensure_socketcan_ready("can0", False)
        authorize.assert_called_once_with("can0", False)

    def test_driver_error_does_not_trigger_authorization(self):
        with patch.object(setup, "_prepare", side_effect=setup._SetupError("Operation not supported")), patch.object(
            setup, "_authorize"
        ) as authorize, self.assertRaisesRegex(ProtocolServiceError, "Operation not supported"):
            setup.ensure_socketcan_ready("can0", True)
        authorize.assert_not_called()

    def test_driver_error_restores_previously_running_classic_interface(self):
        def fail_fd(command):
            if "fd" in command and "on" in command:
                raise setup._SetupError("Operation not supported")

        fake = FakeIp(make_link(up=True, bitrate=500_000, restart_ms=0), fail_fd)
        with patch.object(setup, "_executable", return_value="/usr/sbin/ip"), patch.object(
            setup, "_run", side_effect=fake
        ), patch.object(setup, "_authorize") as authorize, self.assertRaisesRegex(
            ProtocolServiceError, "Operation not supported.*已尝试恢复"
        ):
            setup.ensure_socketcan_ready("can0", True)
        self.assertIn("UP", fake.link["flags"])
        self.assertEqual(fake.link["linkinfo"]["info_data"]["bittiming"]["bitrate"], 500_000)
        self.assertEqual(fake.writes[-1], ["up"])
        self.assertNotIn("fd", fake.writes[-2])
        authorize.assert_not_called()

    def test_up_failure_restores_original_fd_configuration(self):
        failures = [True]

        def fail_first_up(command):
            if command[-1] == "up" and failures:
                failures.pop()
                raise setup._SetupError("driver rejected link up")

        original = make_link(up=True, fd=True, bitrate=500_000, restart_ms=0)
        fake = FakeIp(original, fail_first_up)
        with patch.object(setup, "_executable", return_value="/usr/sbin/ip"), patch.object(
            setup, "_run", side_effect=fake
        ), self.assertRaisesRegex(ProtocolServiceError, "已尝试恢复"):
            setup.ensure_socketcan_ready("can0", False)
        self.assertIn("UP", fake.link["flags"])
        self.assertTrue(setup._is_fd(fake.link))
        self.assertEqual(fake.link["linkinfo"]["info_data"]["bittiming"]["bitrate"], 500_000)
        self.assertEqual(fake.link["linkinfo"]["info_data"]["restart_ms"], 0)

    def test_restore_failure_is_reported(self):
        def fail(command):
            if command[1] == "link" and command[-1] == "up":
                raise setup._SetupError("driver rejected link up")

        fake = FakeIp(make_link(up=True, bitrate=500_000), fail)
        with patch.object(setup, "_executable", return_value="/usr/sbin/ip"), patch.object(
            setup, "_run", side_effect=fake
        ), self.assertRaisesRegex(ProtocolServiceError, "恢复原接口配置失败"):
            setup.ensure_socketcan_ready("can0", False)

    def test_authorization_uses_isolated_python_and_fixed_arguments(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(setup, "_executable", return_value="/usr/bin/pkexec"), patch.object(
            setup.subprocess, "run", return_value=result
        ) as run:
            setup._authorize("can0", True)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/pkexec", "/usr/bin/python3", "-I"])
        self.assertEqual(command[4:], ["--configure", "can0", "fd"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout"], setup.AUTH_TIMEOUT)

    def test_authorization_cancellation_and_missing_agent_are_clear(self):
        for code, text in ((126, "已取消"), (127, "polkit"), (1, "自动配置失败")):
            with self.subTest(code=code), patch.object(setup, "_executable", return_value="/usr/bin/pkexec"), patch.object(
                setup.subprocess, "run", return_value=subprocess.CompletedProcess([], code, "", "failure")
            ), self.assertRaisesRegex(setup._SetupError, text):
                setup._authorize("can0", False)

    def test_authorization_timeout_is_clear(self):
        with patch.object(setup, "_executable", return_value="/usr/bin/pkexec"), patch.object(
            setup.subprocess, "run", side_effect=subprocess.TimeoutExpired("pkexec", setup.AUTH_TIMEOUT)
        ), self.assertRaisesRegex(setup._SetupError, "系统授权超时"):
            setup._authorize("can0", False)

    def test_ip_timeout_is_bounded(self):
        with patch.object(setup.subprocess, "run", side_effect=subprocess.TimeoutExpired("ip", setup.IP_TIMEOUT)) as run:
            with self.assertRaisesRegex(setup._SetupError, "超时"):
                setup._run(["/usr/sbin/ip", "-json"])
        self.assertEqual(run.call_args.kwargs["timeout"], setup.IP_TIMEOUT)

    def test_ip_permissions_are_distinguished_from_driver_errors(self):
        for text, denied in (("RTNETLINK answers: Operation not permitted", True), ("Permission denied", True),
                             ("RTNETLINK answers: Operation not supported", False), ("Invalid argument", False)):
            with self.subTest(text=text), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", text)):
                with self.assertRaises(setup._SetupError) as caught:
                    setup._run(["/usr/sbin/ip", "link"])
            self.assertEqual(caught.exception.permission_denied, denied)

    def test_missing_ip_has_install_instruction(self):
        with patch.object(setup.os.path, "isfile", return_value=False), self.assertRaisesRegex(
            ProtocolServiceError, "iproute2"
        ):
            setup.ensure_socketcan_ready("can0", False)

    def test_invalid_json_is_rejected(self):
        with patch.object(setup, "_run", return_value="not-json"), self.assertRaisesRegex(setup._SetupError, "JSON"):
            setup._read_link("/usr/sbin/ip", "can0")

    def test_helper_invalid_input_works_with_isolated_standard_library(self):
        # Only validation runs; an invalid name is rejected before finding ip.
        result = subprocess.run(
            ["/usr/bin/python3", "-I", str(Path(setup.__file__).resolve()), "--configure", "can0;id", "classic"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("接口名无效", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
