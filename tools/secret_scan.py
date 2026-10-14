"""Secret scan for git and docker images (stdlib only).

  python tools/secret_scan.py git                      tracked files + every commit's added lines (all local branches)
  python tools/secret_scan.py image IMAGE [IMAGE...]   every layer of `docker save IMAGE` (deleted files in lower layers too) + config/history

Flags: private-key PEM blocks (with a real base64 body), secret-looking file names (.key, .pem, .env, *.secret, *.password,
dynamic-security.json, hub-admin credentials), env/history entries that assign SECRET|PASSWORD|TOKEN|KEY a value, and, for git,
quoted literal assignments to password/secret/token names in deploy/ and tools/ (reviewed by hand; tests use fakes).
Exit 1 when anything is found."""
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

PEM = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{80,}")
BAD_NAME = re.compile(r"(^|/)(\.env|[^/]*\.key|[^/]*\.pem|[^/]*\.secret|[^/]*\.password|dynamic-security\.json|id_rsa|id_ed25519)$")
ALLOW_NAME = re.compile(r"(^|/)(certifi/cacert\.pem|etc/ssl/certs/[^/]+\.pem|usr/lib/ssl/cert\.pem)$")      # public CA certificates (no private keys)
LITERAL = re.compile(r"""(?i)\b(password|passwd|secret|token|api[_-]?key)\w*\s*[:=]\s*['"][^'"\s$%{}<>]{12,}['"]""")
ENVBAD = re.compile(r"(?i)(SECRET|PASSWORD|TOKEN|API_?KEY|PRIVATE)[A-Z_]*=.+")
found = []


def flag(where, what):
    found.append(f"{where}: {what}")


def scan_bytes(where, data):
    if PEM.search(data):
        flag(where, "private key PEM block")


def scan_git():
    files = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    for f in filter(None, files):
        if BAD_NAME.search(f) and not ALLOW_NAME.search(f):
            flag(f, "secret-looking file name is tracked")
        try:
            data = open(f, "rb").read()
        except OSError:
            continue
        scan_bytes(f, data)
        if f.startswith(("deploy/", "tools/")) and f.endswith((".ps1", ".sh", ".yml", ".yaml", ".json", ".py", ".md", ".service", ".template")) \
                and f != "tools/secret_scan.py":
            for i, line in enumerate(data.decode("utf-8", "ignore").splitlines(), 1):
                if LITERAL.search(line) and "scan-ok" not in line:
                    flag(f"{f}:{i}", "literal credential-looking assignment: " + line.strip()[:80])
    log = subprocess.check_output(["git", "log", "--all", "-p", "--no-color", "--diff-filter=AM"], errors="ignore")
    if PEM.search(log.encode("utf-8", "ignore")):
        flag("git history", "private key PEM block in some commit")
    names = subprocess.check_output(["git", "log", "--all", "--name-only", "--pretty=format:", "--diff-filter=AM"], errors="ignore").splitlines()
    for n in sorted(set(filter(None, names))):
        if BAD_NAME.search(n) and not ALLOW_NAME.search(n):
            flag("git history", f"secret-looking file name was committed: {n}")


def scan_image(image):
    tmp = tempfile.mkdtemp(prefix="imgscan-")
    tar = os.path.join(tmp, "img.tar")
    subprocess.run(["docker", "save", "-o", tar, image], check=True)
    with tarfile.open(tar) as t:
        names = t.getnames()
        manifest = json.load(t.extractfile("manifest.json"))
        cfg = json.load(t.extractfile(manifest[0]["Config"]))
        for e in cfg.get("config", {}).get("Env") or []:
            if ENVBAD.search(e):
                flag(image, "environment entry looks like a secret: " + e.split("=")[0])
        for h in cfg.get("history", []):
            if ENVBAD.search(h.get("created_by", "")) or re.search(r"(?i)(secret|password|token)\S*\s*=\s*\S{8,}", h.get("created_by", "")):
                flag(image, "build history mentions a credential: " + h["created_by"][:90])
        layers = manifest[0]["Layers"]
        nfiles = 0
        for ly in layers:
            lt = t.extractfile(ly)
            with tarfile.open(fileobj=lt, mode="r|*") as inner:
                for m in inner:
                    nfiles += 1
                    p = m.name
                    if BAD_NAME.search(p) and not ALLOW_NAME.search(p) and not re.search(r"\.(py|pyi)$", p):
                        flag(image, f"layer {ly[:12]} contains {p}")
                    if m.isfile() and m.size < 3_000_000 and not re.search(r"\.so(\.|$)|\.pyc$", p):
                        scan_bytes(f"{image}:{p}", inner.extractfile(m).read())
        print(f"{image}: {len(layers)} layers, {nfiles} entries scanned")
    os.remove(tar)
    os.rmdir(tmp)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "git":
        scan_git()
    else:
        for im in sys.argv[2:]:
            scan_image(im)
    for f in found:
        print("FOUND", f.encode("ascii", "replace").decode())
    print("secret scan:", "FAILED" if found else "clean")
    sys.exit(1 if found else 0)
