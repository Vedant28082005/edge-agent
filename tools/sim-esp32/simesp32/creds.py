"""Provisioning bypass: broker credentials and the CA come from flags/env, exactly what the hub's dynsec client
would have put into the BLE bundle. Passwords are never accepted on the command line (argv leaks) and never logged."""
import os
from pathlib import Path


class CredError(Exception):
    pass


def read_cred_file(path: Path) -> tuple:
    """deploy/mosquitto/dynsec-node-user.ps1 writes `username=...` / `password=...` lines."""
    kv = {}
    for line in Path(path).read_text(encoding="ascii").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    if "username" not in kv or "password" not in kv:
        raise CredError(f"{path.name}: needs username= and password= lines")
    return kv["username"], kv["password"]


def credentials_for(node_id: str, creds_dir, single_node: bool, env=None) -> tuple:
    env = os.environ if env is None else env
    if creds_dir:
        f = Path(creds_dir) / f"{node_id}.cred"
        if f.exists():
            return read_cred_file(f)
    key = "SIM_MQTT_PASSWORD_" + node_id.replace("-", "_").upper()
    if key in env:
        return env.get("SIM_MQTT_USERNAME_" + node_id.replace("-", "_").upper(), node_id), env[key]
    if single_node and "SIM_MQTT_PASSWORD" in env:
        return env.get("SIM_MQTT_USERNAME", node_id), env["SIM_MQTT_PASSWORD"]
    raise CredError(f"no credentials for {node_id}: provide <creds-dir>/{node_id}.cred, or env {key}"
                    + (" / SIM_MQTT_PASSWORD" if single_node else ""))
