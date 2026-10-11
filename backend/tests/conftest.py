import datetime as dt
import io
from pathlib import Path

import pikepdf
import pytest

FIX = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    """The real sample files contain personal data and are git-ignored. On a fresh clone
    they are absent, so tests that need them are skipped instead of failing the build.
    Put Invt.xlsx and sbicap_sample.pdf in tests/fixtures/ to run the full suite."""
    path = FIX / name
    if not path.exists():
        pytest.skip(f"fixture {name} not present (personal data, git-ignored)")
    return path.read_bytes()


@pytest.fixture
def sheet_bytes() -> bytes:
    return _fixture("Invt.xlsx")


@pytest.fixture
def note_bytes() -> bytes:
    return _fixture("sbicap_sample.pdf")


@pytest.fixture
def encrypted_note(note_bytes) -> bytes:
    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(note_bytes)) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(user="EHJPG1234", owner="owner-secret", R=6))
    return out.getvalue()


@pytest.fixture
def app_client(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.config import Config
    from app.container import build

    cfg = Config(data_dir=tmp_path, seed_sheet=None, secret_key=b"test-key", session_hours=1, max_upload_mb=5,
                 live_prices_default=False, static_dir=None, cookie_secure=False)
    container = build(cfg, price_provider=None)
    app = create_app(cfg, container)
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "portfolio-tracker"})
    return client
