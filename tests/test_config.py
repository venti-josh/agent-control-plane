from acp.config import Settings


def test_acp_environment_selects_isolated_data_and_server_url(monkeypatch, tmp_path):
    data_dir = tmp_path / 'custom-data'
    monkeypatch.setenv('ACP_DATA_DIR', str(data_dir))
    monkeypatch.setenv('ACP_URL', 'http://127.0.0.1:9876/')
    settings = Settings.from_env()
    assert settings.data_dir == data_dir
    assert settings.base_url == 'http://127.0.0.1:9876'
    assert data_dir.is_dir()


def test_installed_config_is_independent_of_working_directory(monkeypatch, tmp_path):
    home = tmp_path / 'user-state'
    home.mkdir()
    (home / 'config.env').write_text('DEFAULT_PROVIDER=codex\n')
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    (unrelated / '.env').write_text('DEFAULT_PROVIDER=claude\nACP_DATA_DIR=wrong\n')
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv('ACP_HOME', str(home))
    for key in ('ACP_DATA_DIR', 'ACP_CONFIG_FILE', 'DEFAULT_PROVIDER'):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.default_provider == 'codex'
    assert settings.data_dir == home / 'data'
    assert not (unrelated / 'wrong').exists()


def test_relative_config_paths_are_anchored_to_config_file(monkeypatch, tmp_path):
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'config.env').write_text('ACP_DATA_DIR=state\nDEFAULT_REPO=repo\n')
    other = tmp_path / 'elsewhere'
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setenv('ACP_CONFIG_FILE', str(config / 'config.env'))
    for key in ('ACP_DATA_DIR', 'DEFAULT_REPO'):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.data_dir == config / 'state'
    assert settings.default_repo == str(config / 'repo')
