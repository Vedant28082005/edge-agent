"""Saved Wi-Fi profiles, encrypted at rest (Fernet). The key lives in a file outside the DB."""
from __future__ import annotations

from cryptography.fernet import Fernet

from .db import DB
from .security import SECRETS


class WifiProfiles:
    def __init__(self, db: DB, key: bytes) -> None:
        self.db, self._f = db, Fernet(key)

    def put(self, name: str, ssid: str, password: str) -> None:
        SECRETS.add(password)
        self.db.run("INSERT INTO wifi_profiles(name,ssid,password_enc) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                    "ssid=excluded.ssid, password_enc=excluded.password_enc",
                    (name, ssid, self._f.encrypt(password.encode("utf-8"))))

    def resolve(self, name: str) -> dict | None:
        row = self.db.one("SELECT ssid, password_enc FROM wifi_profiles WHERE name=?", (name,))
        if row is None:
            return None
        pw = self._f.decrypt(bytes(row["password_enc"])).decode("utf-8")
        SECRETS.add(pw)
        return {"ssid": row["ssid"], "password": pw}

    def names(self) -> list[str]:
        return [r["name"] for r in self.db.all("SELECT name FROM wifi_profiles ORDER BY name")]

    def delete(self, name: str) -> bool:
        return self.db.run("DELETE FROM wifi_profiles WHERE name=?", (name,)).rowcount > 0
