"""Local browser fixture. This process performs zero network model calls."""

from uuid import uuid4

import httpx

from backend.app.api import create_app
from backend.app.config import ROOT, Settings
from tests.model_fixture import ModelFixture

app = create_app(
    Settings(data_dir=ROOT / ".cache" / f"browser-fixture-{uuid4().hex}",
             model="local-test-fixture", api_key="fixture-key"),
    model_transport=httpx.MockTransport(ModelFixture(delay=0.06)),
    frontend_dir=ROOT / ".cache" / "account-dist",
)
