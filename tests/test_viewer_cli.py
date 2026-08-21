from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from evolve.cli import app
from evolve.viewer.app import run_viewer


def test_view_defaults(monkeypatch, tmp_path: Path) -> None:
    called = {}

    def capture(workspace: Path, host: str, port_spec: str) -> None:
        called.update(workspace=workspace, host=host, port_spec=port_spec)

    monkeypatch.setattr("evolve.viewer.run_viewer", capture)

    result = CliRunner().invoke(app, ["view", str(tmp_path)])

    assert result.exit_code == 0
    assert called == {"workspace": tmp_path, "host": "127.0.0.1", "port_spec": "8080-8089"}


def test_view_forwards_explicit_host_and_port(monkeypatch, tmp_path: Path) -> None:
    called = {}
    monkeypatch.setattr(
        "evolve.viewer.run_viewer",
        lambda workspace, host, port_spec: called.update(workspace=workspace, host=host, port_spec=port_spec),
    )

    result = CliRunner().invoke(
        app,
        ["view", str(tmp_path), "--host", "0.0.0.0", "--port", "9001"],
    )

    assert result.exit_code == 0
    assert called["host"] == "0.0.0.0"
    assert called["port_spec"] == "9001"


def test_view_catalog_uses_multi_workspace_server(monkeypatch, tmp_path: Path) -> None:
    called = {}
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("experiments: []\n")
    monkeypatch.setattr(
        "evolve.viewer.run_catalog_viewer",
        lambda catalog_path, host, port_spec: called.update(
            catalog=catalog_path,
            host=host,
            port_spec=port_spec,
        ),
    )

    result = CliRunner().invoke(app, ["view", "--catalog", str(catalog), "--port", "9100"])

    assert result.exit_code == 0
    assert called == {"catalog": catalog, "host": "127.0.0.1", "port_spec": "9100"}


def test_run_viewer_prints_rsihub_identity(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr("evolve.viewer.app._select_bindable_port", lambda _host, _ports: 8765)
    monkeypatch.setattr("evolve.viewer.app.create_viewer_app", lambda _workspace: object())
    monkeypatch.setattr("evolve.viewer.app.uvicorn.run", lambda *_args, **_kwargs: None)

    run_viewer(tmp_path, "127.0.0.1", "8765")

    assert capsys.readouterr().out.startswith("RSIHub viewer: http://127.0.0.1:8765\n")
