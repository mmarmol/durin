import asyncio

from loguru import logger
from durin.channels import qq as qqmod


def test_read_media_bytes_offloads_url_validation(monkeypatch):
    seen = {"thread": False}
    real_to_thread = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        seen["thread"] = True
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("durin.channels.qq.asyncio.to_thread", spy)

    ch = qqmod.QQChannel.__new__(qqmod.QQChannel)
    ch.logger = logger
    ch._http = None

    data, name = asyncio.run(ch._read_media_bytes("http://10.0.0.1/x.png"))
    assert seen["thread"] is True
    assert data is None and name is None
