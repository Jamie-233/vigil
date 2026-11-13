import pytest
import yaml

from vigil.config import ConfigError
from vigil.config_file import ConfigFile


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "vigil.yml"
    path.write_text("defaults: {interval: 60}\ntargets:\n  - {name: a, type: tcp, address: 'h:1'}\n")
    return ConfigFile(path)


def names(cfg):
    return [t["name"] for t in yaml.safe_load(cfg.path.read_text())["targets"]]


def test_add_validates_and_keeps_defaults(cfg):
    t = cfg.add({"name": "b", "type": "http", "address": "https://x/", "options": {"expect_body": "ok", "bogus": 1}})
    assert t.interval == 60 and t.options == {"expect_body": "ok"}
    assert names(cfg) == ["a", "b"]
    assert cfg.path.with_suffix(".yml.bak").exists()


def test_invalid_change_leaves_file_untouched(cfg):
    before = cfg.path.read_text()
    with pytest.raises(ConfigError):
        cfg.add({"name": "b", "type": "tcp", "address": "h:1", "interval": 5, "timeout": 10})
    with pytest.raises(ConfigError):
        cfg.add({"name": "a", "type": "tcp", "address": "h:2"})
    assert cfg.path.read_text() == before


def test_replace_and_remove(cfg):
    cfg.add({"name": "b", "type": "icmp", "address": "192.0.2.1"})
    cfg.replace("b", {"name": "c", "type": "icmp", "address": "192.0.2.2"})
    assert names(cfg) == ["a", "c"]
    cfg.remove("a")
    with pytest.raises(ConfigError):
        cfg.remove("c")
    with pytest.raises(KeyError):
        cfg.remove("missing")
