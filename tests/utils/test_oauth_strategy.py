from durin.utils import oauth


def test_device_code_when_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert oauth.should_use_device_code() is True


def test_loopback_when_local_gui(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(oauth.sys, "platform", "darwin")
    assert oauth.should_use_device_code() is False
