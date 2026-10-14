import pytest

from simesp32.cli import main
from simesp32.config import ConfigError, load_config
from simesp32.creds import CredError, credentials_for

NID = "esp32-a1b2c3d4e501"


def test_creds_dir_file(tmp_path):
    (tmp_path / f"{NID}.cred").write_text(f"username={NID}\npassword=s3cret\n", encoding="ascii")
    assert credentials_for(NID, tmp_path, single_node=False, env={}) == (NID, "s3cret")


def test_creds_from_per_node_env():
    env = {"SIM_MQTT_PASSWORD_ESP32_A1B2C3D4E501": "pw"}
    assert credentials_for(NID, None, False, env) == (NID, "pw")


def test_single_node_env_fallback_only_for_one_node():
    env = {"SIM_MQTT_PASSWORD": "pw"}
    assert credentials_for(NID, None, True, env) == (NID, "pw")
    with pytest.raises(CredError):
        credentials_for(NID, None, False, env)


def test_missing_creds_error_names_the_options_but_not_a_password(tmp_path):
    with pytest.raises(CredError) as e:
        credentials_for(NID, tmp_path, True, {})
    assert ".cred" in str(e.value)


def test_no_password_flag_exists():
    from simesp32.cli import build_parser
    opts = {o for a in build_parser()._actions for o in a.option_strings}
    assert not any("pass" in o for o in opts)          # secrets never travel on argv


def test_cli_requires_ca_for_real_broker(capsys, tmp_path):
    cfg = tmp_path / "n.yaml"
    cfg.write_text("nodes:\n  - node_id: esp32-a1b2c3d4e501\n    endpoints:\n      - {local_id: l1, type: light, "
                   "capabilities: [{id: power, boot_value: false}]}\n", encoding="utf-8")
    assert main([str(cfg)]) == 2
    assert "--ca" in capsys.readouterr().err


def test_cli_check_mode_and_bad_yaml(capsys, tmp_path):
    good = tmp_path / "g.yaml"
    good.write_text("nodes:\n  - node_id: esp32-a1b2c3d4e501\n    endpoints:\n      - {local_id: l1, type: light, "
                    "capabilities: [{id: power, boot_value: false}]}\n", encoding="utf-8")
    assert main([str(good), "--check"]) == 0
    bad = tmp_path / "b.yaml"
    bad.write_text("nodes:\n  - node_id: esp32-a1b2c3d4e501\n    endpoints:\n      - {local_id: l1, type: light, "
                   "capabilities: [{id: teleport, boot_value: false}]}\n", encoding="utf-8")
    assert main([str(bad), "--check"]) == 2
    assert "vocabulary" in capsys.readouterr().err


def test_duplicate_node_ids_rejected(tmp_path):
    f = tmp_path / "d.yaml"
    n = ("  - node_id: esp32-a1b2c3d4e501\n    endpoints:\n      - {local_id: l1, type: light, "
         "capabilities: [{id: power, boot_value: false}]}\n")
    f.write_text("nodes:\n" + n + n, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(f)


def test_username_must_be_node_id(tmp_path):
    (tmp_path / f"{NID}.cred").write_text("username=someone-else\npassword=x\n", encoding="ascii")
    with pytest.raises(CredError):
        credentials_for(NID, tmp_path, False, {})
    assert credentials_for(NID, None, True, {"SIM_MQTT_PASSWORD": "p", "SIM_MQTT_USERNAME": "other"}) == (NID, "p")


def test_cli_dedup_size_flag(capsys, tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("nodes:\n  - node_id: esp32-a1b2c3d4e501\n    endpoints:\n      - {local_id: l1, type: light, "
                 "capabilities: [{id: power, boot_value: false}]}\n", encoding="utf-8")
    assert main([str(f), "--check", "--dedup-size", "0"]) == 2
    assert main([str(f), "--check", "--dedup-size", "8"]) == 0
