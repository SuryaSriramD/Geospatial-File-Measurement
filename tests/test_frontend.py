"""The complete browser interface is served by the API itself."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_frontend_and_its_assets_are_available_on_same_server(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert 'href="/assets/app.css"' in page.text
        assert 'src="/assets/app.js"' in page.text
        for asset, media_type in (("app.css", "text/css"), ("app.js", "javascript")):
            response = client.get(f"/assets/{asset}")
            assert response.status_code == 200
            assert media_type in response.headers["content-type"]
        assert client.get("/assets/../main.py").status_code == 404
