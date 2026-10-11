import os, sys, tempfile, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGYLAB_HOME", str(tmp_path))
    return tmp_path
