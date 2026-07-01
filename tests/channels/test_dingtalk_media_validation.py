import asyncio

from durin.channels import dingtalk as dt_mod


class _FakeDingTalk:
    _validate_remote_media_url = dt_mod.DingTalkChannel._validate_remote_media_url
    _fetch_remote_media_bytes = dt_mod.DingTalkChannel._fetch_remote_media_bytes

    def __init__(self):
        self._http = object()
        import logging
        self.logger = logging.getLogger("test")


def test_fetch_offloads_url_validation(monkeypatch):
    ch = _FakeDingTalk()
    seen = {"thread": False}
    real_to_thread = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        seen["thread"] = True
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("durin.channels.dingtalk.asyncio.to_thread", spy)
    monkeypatch.setattr(
        _FakeDingTalk, "_validate_remote_media_url", lambda self, ref: False, raising=False
    )
    data, err = asyncio.run(ch._fetch_remote_media_bytes("http://10.0.0.1/x.png"))
    assert seen["thread"] is True
    assert data is None
