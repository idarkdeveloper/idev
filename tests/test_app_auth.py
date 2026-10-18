import hmac
import json
import os
import stat

import pytest

from trading_agent import app_auth, cli
from trading_agent.app_auth import Authenticator, add_device, list_devices, revoke


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_add_returns_a_long_secret_and_stores_only_a_hash(tmp_path):
    secret = add_device(tmp_path, "phone")
    assert len(secret) >= 43
    path = tmp_path / app_auth.DEVICES_FILE
    assert secret not in path.read_text(encoding="utf-8")
    assert "phone" in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list_devices(tmp_path)[0].keys() == {"name", "created"}


def test_correct_secret_accepted_wrong_or_missing_rejected(tmp_path):
    secret = add_device(tmp_path, "phone")
    auth = Authenticator(tmp_path)
    assert auth.check(secret, "1.1.1.1") == ("phone", 200)
    assert auth.check("wrong", "1.1.1.1") == (None, 401)
    assert auth.check(None, "1.1.1.1") == (None, 401)
    assert auth.check("", "1.1.1.1") == (None, 401)


def test_comparison_is_timing_safe(tmp_path, monkeypatch):
    secret = add_device(tmp_path, "phone")
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(app_auth.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert Authenticator(tmp_path).check(secret, "c")[1] == 200
    assert calls


def test_five_failures_lock_the_client_for_fifteen_minutes_then_release(tmp_path):
    secret = add_device(tmp_path, "phone")
    clock = Clock()
    auth = Authenticator(tmp_path, clock=clock)
    for _ in range(5):
        assert auth.check("bad", "9.9.9.9") == (None, 401)
    assert auth.check(secret, "9.9.9.9") == (None, 429)   # even the right secret is blocked
    clock.t += 899
    assert auth.check(secret, "9.9.9.9")[1] == 429
    clock.t += 2
    assert auth.check(secret, "9.9.9.9") == ("phone", 200)


def test_lockout_is_per_client_address(tmp_path):
    secret = add_device(tmp_path, "phone")
    auth = Authenticator(tmp_path, clock=Clock())
    for _ in range(5):
        auth.check("bad", "9.9.9.9")
    assert auth.check(secret, "9.9.9.9")[1] == 429
    assert auth.check(secret, "8.8.8.8") == ("phone", 200)


def test_failures_older_than_ten_minutes_do_not_count(tmp_path):
    secret = add_device(tmp_path, "phone")
    clock = Clock()
    auth = Authenticator(tmp_path, clock=clock)
    for _ in range(4):
        auth.check("bad", "c")
    clock.t += 601
    auth.check("bad", "c")
    assert auth.check(secret, "c")[1] == 200


def test_rate_limit_61st_request_in_a_minute_gets_429(tmp_path):
    secret = add_device(tmp_path, "phone")
    clock = Clock()
    auth = Authenticator(tmp_path, clock=clock)
    for _ in range(60):
        assert auth.check(secret, "c")[1] == 200
    assert auth.check(secret, "c") == (None, 429)
    clock.t += 61
    assert auth.check(secret, "c")[1] == 200


def test_revoke_one_and_all(tmp_path):
    a = add_device(tmp_path, "a")
    b = add_device(tmp_path, "b")
    assert revoke(tmp_path, "a") == 1
    assert Authenticator(tmp_path).check(a, "c") == (None, 401)
    assert Authenticator(tmp_path).check(b, "c")[1] == 200
    assert revoke(tmp_path, "nobody") == 0
    add_device(tmp_path, "c")
    assert revoke(tmp_path, all_=True) == 2
    assert list_devices(tmp_path) == []


def test_duplicate_name_is_refused(tmp_path):
    add_device(tmp_path, "phone")
    with pytest.raises(ValueError):
        add_device(tmp_path, "phone")
    assert len(list_devices(tmp_path)) == 1


def test_garbage_devices_file_means_nobody_is_accepted(tmp_path):
    (tmp_path / app_auth.DEVICES_FILE).write_text("{not json", encoding="utf-8")
    assert Authenticator(tmp_path).check("anything", "c") == (None, 401)
    (tmp_path / app_auth.DEVICES_FILE).write_text(json.dumps([1, "x", {"name": 5}]), encoding="utf-8")
    assert Authenticator(tmp_path).check("anything", "c") == (None, 401)
    assert list_devices(tmp_path) == []


def test_cli_add_list_revoke_use_the_given_state_folder(tmp_path, capsys):
    state = str(tmp_path)
    assert cli.main(["app-device", "--state-dir", state, "add", "phone"]) == 0
    out = capsys.readouterr().out
    assert "Copy it now; it is not shown again." in out
    stored = json.loads((tmp_path / app_auth.DEVICES_FILE).read_text(encoding="utf-8"))
    secret_in_output = [w for w in out.split() if len(w) >= 43][0]
    assert secret_in_output not in json.dumps(stored)
    assert Authenticator(tmp_path).check(secret_in_output, "c")[0] == "phone"
    assert cli.main(["app-device", "--state-dir", state, "list"]) == 0
    listing = capsys.readouterr().out
    assert "phone" in listing and secret_in_output not in listing
    assert cli.main(["app-device", "--state-dir", state, "add", "phone"]) == 1
    assert cli.main(["app-device", "--state-dir", state, "revoke", "phone"]) == 0
    assert cli.main(["app-device", "--state-dir", state, "revoke", "phone"]) == 1
    cli.main(["app-device", "--state-dir", state, "add", "x"])
    assert cli.main(["app-device", "--state-dir", state, "revoke", "--all"]) == 0
    assert list_devices(tmp_path) == []


def test_bad_input_is_a_counted_401_never_a_crash(tmp_path):
    secret = add_device(tmp_path, "phone")
    path = tmp_path / app_auth.DEVICES_FILE
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows.append({"name": "evil", "hash": "é", "created": "2026-01-01"})
    path.write_text(json.dumps(rows), encoding="utf-8")
    assert [d["name"] for d in list_devices(tmp_path)] == ["phone"]   # non-hex hash row dropped
    auth = Authenticator(tmp_path, clock=Clock())
    for _ in range(5):
        assert auth.check("\ud800", "c") == (None, 401)   # lone surrogate
    assert auth.check(secret, "c") == (None, 429)   # the attempts counted toward lockout


def test_limited_results_never_carry_a_device_name(tmp_path):
    secret = add_device(tmp_path, "phone")
    auth = Authenticator(tmp_path, clock=Clock())
    for _ in range(60):
        auth.check(secret, "c")
    assert auth.check(secret, "c") == (None, 429)


def test_unreadable_devices_file_is_never_overwritten(tmp_path, monkeypatch):
    add_device(tmp_path, "keep")
    before = (tmp_path / app_auth.DEVICES_FILE).read_bytes()

    def boom(self, *a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(app_auth.Path, "read_text", boom)
    with pytest.raises(RuntimeError):
        add_device(tmp_path, "new")
    with pytest.raises(RuntimeError):
        revoke(tmp_path, all_=True)
    assert Authenticator(tmp_path).check("x", "c") == (None, 401)
    monkeypatch.undo()
    assert (tmp_path / app_auth.DEVICES_FILE).read_bytes() == before


def test_lockout_memory_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(app_auth, "MAX_TRACKED", 10)
    clock = Clock()
    auth = Authenticator(tmp_path, clock=clock)
    for i in range(50):
        auth.check("bad", f"ip{i}")
    assert len(auth._fails) <= 11
    for i in range(5):
        auth.check("bad", "target")
    clock.t += 2000   # everything expired: the next call sweeps it away
    for i in range(12):
        auth.check("bad", f"more{i}")
    assert len(auth._fails) <= 11 and len(auth._blocked) == 0
