"""Unit tests for the hosts-file parsing in ``bulk-show.py``.

``bulk-show.py`` is not importable by name (the hyphen is not a valid module
identifier), so we load it through :mod:`importlib` and exercise the pure
parsing helpers without touching SSH.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BULK_SHOW_PATH = REPO_ROOT / "bulk-show.py"


def _load_bulk_show():
    spec = importlib.util.spec_from_file_location("bulk_show", BULK_SHOW_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


bulk_show = _load_bulk_show()


class NormalizeDeviceTypeTests(unittest.TestCase):
    def test_known_aliases_map_to_canonical(self) -> None:
        self.assertEqual(bulk_show.normalize_device_type("edge"), bulk_show.DEVICE_EDGE)
        self.assertEqual(bulk_show.normalize_device_type("cedge"), bulk_show.DEVICE_EDGE)
        # vEdge runs the viptela CLI, so it uses the controller profile
        # (paginate false, no shell step).
        for alias in ("controller", "ctrl", "vsmart", "vbond", "vmanage", "vedge"):
            self.assertEqual(
                bulk_show.normalize_device_type(alias),
                bulk_show.DEVICE_CONTROLLER,
                msg=alias,
            )

    def test_case_and_whitespace_insensitive(self) -> None:
        self.assertEqual(
            bulk_show.normalize_device_type("  VSmart "), bulk_show.DEVICE_CONTROLLER
        )

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(bulk_show.normalize_device_type("router"))
        self.assertIsNone(bulk_show.normalize_device_type(None))


class StripAnsiTests(unittest.TestCase):
    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(bulk_show.strip_ansi("vsmart# "), "vsmart# ")

    def test_strips_dec_autowrap_and_sgr(self) -> None:
        # The viptela CLI emits "\x1b[?7h" before its prompt; color codes too.
        self.assertEqual(bulk_show.strip_ansi("\x1b[?7hvsmart#"), "vsmart#")
        self.assertEqual(bulk_show.strip_ansi("\x1b[0mhi\x1b[1;32mthere"), "hithere")


class ExtractPromptTests(unittest.TestCase):
    def test_plain_viptela_prompt(self) -> None:
        self.assertEqual(bulk_show.extract_prompt("banner\nvsmart# "), "vsmart#")

    def test_prompt_with_ansi_escape_is_cleaned(self) -> None:
        # Regression: viptela controllers prefix the prompt with "\x1b[?7h".
        # Without stripping, the captured prompt would be "\x1b[?7hvsmart#"
        # which then fails to match subsequent (clean) prompts.
        self.assertEqual(
            bulk_show.extract_prompt("login banner\n\x1b[?7hvsmart# "),
            "vsmart#",
        )


class BuildCommandPromptReTests(unittest.TestCase):
    def test_controller_prompt_matches_after_command(self) -> None:
        # End-to-end of the bug: capture a prompt that arrived with an ANSI
        # escape, then ensure the derived regex matches a later clean prompt.
        captured = bulk_show.extract_prompt("\x1b[?7hvsmart# ")
        cmd_re = bulk_show.build_command_prompt_re(captured)
        self.assertRegex("...output...\nvsmart# ", cmd_re)
        self.assertRegex("vsmart(config)# ", cmd_re)


class FakeChannel:
    """Minimal paramiko-like channel that yields preset byte chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def settimeout(self, _timeout):
        pass

    def send(self, _data):
        return len(_data)

    def recv(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        import socket as _socket

        raise _socket.timeout()


class _FakeSSHClient:
    """Minimal paramiko.SSHClient stand-in returning a preset shell channel."""

    def __init__(self, channel):
        self._channel = channel

    def load_system_host_keys(self):
        pass

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, *_args, **_kwargs):
        pass

    def invoke_shell(self):
        return self._channel

    def close(self):
        pass


def _make_fake_paramiko(channel):
    """Build a stub ``paramiko`` module exposing only what connect_and_execute
    touches, wired to return ``channel`` from ``invoke_shell``."""
    return types.SimpleNamespace(
        SSHClient=lambda: _FakeSSHClient(channel),
        AutoAddPolicy=lambda: object(),
        RejectPolicy=lambda: object(),
        AuthenticationException=type("AuthenticationException", (Exception,), {}),
        SSHException=type("SSHException", (Exception,), {}),
    )


@contextlib.contextmanager
def _injected_paramiko(channel):
    """Temporarily install a stub ``paramiko`` so the lazy import inside
    connect_and_execute picks it up instead of the real library."""
    saved = sys.modules.get("paramiko")
    sys.modules["paramiko"] = _make_fake_paramiko(channel)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        if saved is not None:
            sys.modules["paramiko"] = saved
        else:
            sys.modules.pop("paramiko", None)


def _parse_capture_stderr(line):
    """Call parse_host_line, returning ``(result, captured_stderr)``."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result = bulk_show.parse_host_line(line)
    return result, err.getvalue()


class ReadChannelControllerTests(unittest.TestCase):
    def test_prompt_detected_despite_ansi_escape(self) -> None:
        chan = FakeChannel([b"\x1b[?7hvsmart# "])
        buf, kind = bulk_show.read_channel(
            chan, idle_timeout=0.05, max_wait=2.0, poll_interval=0.01
        )
        self.assertEqual(kind, bulk_show.MATCH_PROMPT)
        self.assertEqual(buf, "vsmart# ")


class ReadUntilPromptTests(unittest.TestCase):
    def test_late_prompt_recovered_by_nudge(self) -> None:
        # First read yields output that goes idle without a prompt; after the
        # newline nudge the device redraws its prompt and completion is
        # confirmed (MATCH_PROMPT), not misreported as a timeout.
        cmd_re = bulk_show.build_command_prompt_re("RT01#")
        chan = FakeChannel([b"show sdwan control connections\n<table>\n", b"RT01#"])
        buf, kind = bulk_show.read_until_prompt(
            chan, prompt_re=cmd_re, idle_timeout=0.05, max_wait=1.0, nudge_wait=1.0
        )
        self.assertEqual(kind, bulk_show.MATCH_PROMPT)
        self.assertTrue(buf.rstrip().endswith("RT01#"))

    def test_gives_up_after_bounded_nudges(self) -> None:
        # A device that never returns a prompt must not spin forever: the
        # nudges are bounded and a non-prompt result is returned.
        cmd_re = bulk_show.build_command_prompt_re("RT01#")
        chan = FakeChannel([b"partial output with no prompt\n"])
        buf, kind = bulk_show.read_until_prompt(
            chan,
            prompt_re=cmd_re,
            idle_timeout=0.05,
            max_wait=0.3,
            nudge_attempts=2,
            nudge_wait=0.2,
        )
        self.assertIn(kind, (bulk_show.MATCH_IDLE, bulk_show.MATCH_MAX_WAIT))
        self.assertIn("partial output", buf)


class PagerHandlingTests(unittest.TestCase):
    def test_drains_more_and_end_prompts(self) -> None:
        # Simulate a config-mode pager: a "--More--" mid-output prompt, then an
        # "(END)" prompt, then the real config prompt. read_channel must page
        # through both and settle on MATCH_PROMPT with all content captured.
        cmd_re = bulk_show.build_command_prompt_re("RT01#")
        chan = FakeChannel(
            [
                b"header line\n--More--",
                b"\r        \rmiddle line\n(END)",
                b"\rRT01(config)# ",
            ]
        )
        buf, kind = bulk_show.read_channel(
            chan, prompt_re=cmd_re, idle_timeout=0.05, max_wait=2.0, poll_interval=0.01
        )
        self.assertEqual(kind, bulk_show.MATCH_PROMPT)
        self.assertIn("header line", buf)
        self.assertIn("middle line", buf)

    def test_clean_command_output_scrubs_pager_noise(self) -> None:
        raw = "row1\n--More--\r        \rrow2\n(END)\rRT01(config)# "
        cleaned = bulk_show.clean_command_output(raw)
        self.assertNotIn("--More--", cleaned)
        self.assertNotIn("(END)", cleaned)
        self.assertNotIn("\r", cleaned)
        self.assertIn("row1", cleaned)
        self.assertIn("row2", cleaned)


class ParseHostLineTests(unittest.TestCase):
    def test_blank_and_comment_lines_return_none(self) -> None:
        self.assertIsNone(bulk_show.parse_host_line(""))
        self.assertIsNone(bulk_show.parse_host_line("   \n"))
        self.assertIsNone(bulk_show.parse_host_line("# a comment"))
        self.assertIsNone(bulk_show.parse_host_line("   # indented comment"))

    def test_two_column_edge_default(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_EDGE),
        )

    def test_three_column_edge_with_password(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,secret"),
            ("2.1.1.1", "admin", "secret", bulk_show.DEVICE_EDGE),
        )

    def test_bare_keyword_controller_no_password(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,controller"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_CONTROLLER),
        )

    def test_bare_keyword_controller_with_password(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,secret,vsmart"),
            ("2.1.1.1", "admin", "secret", bulk_show.DEVICE_CONTROLLER),
        )

    def test_explicit_type_token_no_password(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,type=controller"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_CONTROLLER),
        )

    def test_explicit_type_token_with_password(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,secret,type=vbond"),
            ("2.1.1.1", "admin", "secret", bulk_show.DEVICE_CONTROLLER),
        )

    def test_explicit_type_edge_with_password(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,secret,type=edge"),
            ("2.1.1.1", "admin", "secret", bulk_show.DEVICE_EDGE),
        )

    def test_whitespace_is_stripped(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("  2.1.1.1 , admin , secret , controller "),
            ("2.1.1.1", "admin", "secret", bulk_show.DEVICE_CONTROLLER),
        )

    def test_missing_username_raises(self) -> None:
        with self.assertRaises(ValueError):
            bulk_show.parse_host_line("2.1.1.1")

    def test_unknown_explicit_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            bulk_show.parse_host_line("2.1.1.1,admin,type=router")

    def test_too_many_fields_raises(self) -> None:
        with self.assertRaises(ValueError):
            bulk_show.parse_host_line("2.1.1.1,admin,secret,extra,controller")

    # -- Finding 1: bare trailing keyword is consumed but no longer silently --
    def test_bare_keyword_three_col_infers_type_and_warns(self) -> None:
        # Backward-compat caveat: "ip,user,vmanage" keeps treating 'vmanage'
        # as a device type (password left empty), but now warns about it.
        result, err = _parse_capture_stderr("4.1.1.1,admin,vmanage")
        self.assertEqual(
            result, ("4.1.1.1", "admin", None, bulk_show.DEVICE_CONTROLLER)
        )
        self.assertIn("vmanage", err)
        self.assertIn("device type", err)

    def test_bare_keyword_edge_three_col_infers_type_and_warns(self) -> None:
        result, err = _parse_capture_stderr("4.1.1.1,admin,edge")
        self.assertEqual(
            result, ("4.1.1.1", "admin", None, bulk_show.DEVICE_EDGE)
        )
        self.assertIn("edge", err)

    def test_type_workaround_preserves_collision_password(self) -> None:
        # The documented escape hatch: "type=" makes the keyword-looking value
        # unambiguously a password, and no warning is emitted (4-column form).
        result, err = _parse_capture_stderr("4.1.1.1,admin,vmanage,type=edge")
        self.assertEqual(
            result, ("4.1.1.1", "admin", "vmanage", bulk_show.DEVICE_EDGE)
        )
        self.assertEqual(err, "")

    # -- Finding 3: conflicting explicit type= tokens ---------------------- --
    def test_conflicting_explicit_types_raise(self) -> None:
        with self.assertRaises(ValueError):
            bulk_show.parse_host_line("2.1.1.1,admin,type=edge,type=vsmart")

    def test_repeated_same_canonical_type_ok(self) -> None:
        # type=controller and type=vbond both map to DEVICE_CONTROLLER, so the
        # pair is consistent and accepted.
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,type=controller,type=vbond"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_CONTROLLER),
        )

    # -- Finding 5: additional coverage ----------------------------------- --
    def test_empty_password_three_col_is_none(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_EDGE),
        )

    def test_empty_password_with_controller_is_none(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,,controller"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_CONTROLLER),
        )

    def test_empty_username_raises(self) -> None:
        with self.assertRaises(ValueError):
            bulk_show.parse_host_line("2.1.1.1,,secret")

    def test_unknown_trailing_keyword_is_password(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,notatype"),
            ("2.1.1.1", "admin", "notatype", bulk_show.DEVICE_EDGE),
        )

    def test_explicit_type_is_case_insensitive(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("2.1.1.1,admin,TYPE=VSmart"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_CONTROLLER),
        )

    def test_explicit_type_token_order_independent(self) -> None:
        self.assertEqual(
            bulk_show.parse_host_line("type=controller,2.1.1.1,admin"),
            ("2.1.1.1", "admin", None, bulk_show.DEVICE_CONTROLLER),
        )


class ResolveCommandsFileTests(unittest.TestCase):
    def test_controller_prefers_controller_file(self) -> None:
        self.assertEqual(
            bulk_show.resolve_commands_file(
                bulk_show.DEVICE_CONTROLLER, "base.txt", "ctrl.txt", "edge.txt"
            ),
            "ctrl.txt",
        )

    def test_edge_prefers_edge_file(self) -> None:
        self.assertEqual(
            bulk_show.resolve_commands_file(
                bulk_show.DEVICE_EDGE, "base.txt", "ctrl.txt", "edge.txt"
            ),
            "edge.txt",
        )

    def test_controller_falls_back_to_base(self) -> None:
        self.assertEqual(
            bulk_show.resolve_commands_file(
                bulk_show.DEVICE_CONTROLLER, "base.txt", None, "edge.txt"
            ),
            "base.txt",
        )

    def test_edge_falls_back_to_base(self) -> None:
        self.assertEqual(
            bulk_show.resolve_commands_file(
                bulk_show.DEVICE_EDGE, "base.txt", "ctrl.txt", None
            ),
            "base.txt",
        )

    def test_returns_none_when_no_file_available(self) -> None:
        self.assertIsNone(
            bulk_show.resolve_commands_file(
                bulk_show.DEVICE_CONTROLLER, None, None, None
            )
        )


class ControllerConnectTests(unittest.TestCase):
    """Finding 2: a controller that re-prompts for a password must fail loudly
    instead of silently succeeding with show commands sent into the prompt."""

    def test_password_reprompt_reports_failure(self) -> None:
        chan = FakeChannel([b"\nviptela login banner\nPassword: "])
        with _injected_paramiko(chan):
            result = bulk_show.connect_and_execute(
                "9.9.9.9",
                "admin",
                "pw",
                "commands-not-read.txt",
                {},
                device_type=bulk_show.DEVICE_CONTROLLER,
            )
        self.assertNotEqual(result["status"], bulk_show.SESSION_OK)
        self.assertEqual(result["status"], bulk_show.SESSION_AUTH_SHELL)
        self.assertEqual(result["commands"], [])

    def test_auth_failure_tail_reports_failure(self) -> None:
        chan = FakeChannel([b"\nLogin incorrect\n"])
        with _injected_paramiko(chan):
            result = bulk_show.connect_and_execute(
                "9.9.9.9",
                "admin",
                "pw",
                "commands-not-read.txt",
                {},
                device_type=bulk_show.DEVICE_CONTROLLER,
            )
        self.assertEqual(result["status"], bulk_show.SESSION_AUTH_SHELL)
        self.assertIn("rejected", result["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
