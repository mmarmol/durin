import pytest
from durin.service.principal import Principal
from durin.service.sessions import SessionsService, SessionMessagesQuery
from durin.service.types import NotFoundError


class _SM:
    def read_session_file(self, key):
        return {"messages": []} if key.startswith(("subagent:", "workflow:")) else None


@pytest.mark.asyncio
async def test_messages_accepts_subagent_and_workflow_keys():
    svc = SessionsService(session_manager=_SM())
    res = await svc.messages(SessionMessagesQuery(key="subagent:t1"), Principal.local())
    assert res.data == {"messages": []}
    res2 = await svc.messages(SessionMessagesQuery(key="workflow:r9:node1:1"), Principal.local())
    assert res2.data == {"messages": []}


@pytest.mark.asyncio
async def test_messages_still_rejects_unknown_prefix():
    svc = SessionsService(session_manager=_SM())
    with pytest.raises(NotFoundError):
        await svc.messages(SessionMessagesQuery(key="memory:secret"), Principal.local())
