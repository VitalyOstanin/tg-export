import pytest_asyncio

from tg_export.state import ExportState


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()
