"""deploy/vlmpi kit: shell syntax, LF endings, re-run safety, and the pre-copy `bash -n` gate in tools/vlmpi-deploy.ps1."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "deploy" / "vlmpi"
PS1 = ROOT / "tools" / "vlmpi-deploy.ps1"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


def bash():
    return str(GIT_BASH) if GIT_BASH.exists() else shutil.which("bash")


def powershell():
    return shutil.which("powershell") or shutil.which("pwsh")


needs_bash = pytest.mark.skipif(bash() is None, reason="bash not available")
needs_ps = pytest.mark.skipif(powershell() is None or not GIT_BASH.exists(),
                              reason="needs Windows PowerShell and Git bash")


def bash_n(path):
    return subprocess.run([bash(), "-n", str(path)], capture_output=True, text=True)


@needs_bash
def test_every_kit_script_passes_bash_n():
    scripts = sorted(KIT.glob("*.sh"))
    assert scripts
    for s in scripts:
        r = bash_n(s)
        assert r.returncode == 0, f"{s.name}: {r.stderr}"


@needs_bash
def test_bash_n_catches_the_original_bug(tmp_path):
    """An apostrophe inside ${VAR:?word} opens a quote that never closes."""
    bad = tmp_path / "bad.sh"
    bad.write_bytes(b'#!/usr/bin/env bash\nBIND="${1:?usage: x <this node\'s ip>}"\necho ok\n')
    r = bash_n(bad)
    assert r.returncode != 0 and "unexpected EOF" in r.stderr


def test_kit_files_are_lf_only_and_the_installer_is_ascii():
    for p in KIT.iterdir():
        assert b"\r" not in p.read_bytes(), f"{p.name} has CR"
    assert all(b < 128 for b in (KIT / "install.sh").read_bytes())


def test_gitattributes_forces_lf():
    ga = (ROOT / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in ga and "deploy/vlmpi/* text eol=lf" in ga


def test_install_sh_never_starts_services_or_writes_the_verifier():
    text = (KIT / "install.sh").read_text()
    assert "set -euo pipefail" in text
    for line in text.splitlines():
        code = line.split("#")[0]
        if code.lstrip().startswith("echo"):
            continue
        assert not re.search(r"systemctl.*(start|enable|restart)|sudo ", code), line
        assert not re.search(r">\s*\S*agent\.sha256", code), line


LLAMA_ENV = "VLM_BIN=/usr/bin/env\nVLM_CTX=999\n"


def exec_first_words(unit: Path) -> list:
    return [ln.split("=", 1)[1].split()[0] for ln in unit.read_text().splitlines() if ln.startswith("ExecStart=")]


PYTHON3_STUB = """#!/bin/bash
# `python3 -c ...` succeeds; `python3 -m venv DIR` makes DIR/bin/python, which accepts pip and the stdin self-check.
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'EOS'
#!/bin/bash
if [ "$1" = "-" ]; then cat > /dev/null; echo "config + contract OK: stub"; fi
exit 0
EOS
  chmod +x "$3/bin/python"
fi
exit 0
"""


@needs_bash
def test_install_sh_rerun_keeps_secrets_and_config_and_starts_nothing(tmp_path):
    """Run the real script twice in a fake HOME with stubbed python3/systemctl (no network, no root)."""
    home = tmp_path / "home"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "systemctl.log"
    (stubs / "systemctl").write_text('#!/bin/bash\necho "$@" >> "%s"\n' % log.as_posix(), newline="\n")
    (stubs / "python3").write_text(PYTHON3_STUB, newline="\n")
    stage = tmp_path / "stage"
    shutil.copytree(KIT, stage / "deploy" / "vlmpi")
    pkg = stage / "services" / "vision-node" / "vision_node"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (stage / "contracts" / "openapi").mkdir(parents=True)
    (stage / "contracts" / "openapi" / "vision-node-internal.yaml").write_text("openapi: 3.1.0\n")
    base = home / "edge-vision"
    (base / "secrets" / "verifiers").mkdir(parents=True)
    (base / "config").mkdir()
    (base / "secrets" / "verifiers" / "agent.sha256").write_text("a" * 64)
    (base / "config" / "vision-node.json").write_text('{"keep": "me"}')
    (base / "config" / "llama.env").write_text(LLAMA_ENV)
    script = (stage / "deploy" / "vlmpi" / "install.sh").as_posix()
    # the stubs are bash scripts: run them through bash by putting a tiny wrapper dir first on PATH
    run_env = {"HOME": str(home), "PATH": f"{stubs.as_posix()}:/usr/bin:/bin", "SystemRoot": r"C:\Windows"}

    def run():
        return subprocess.run([bash(), script, "100.64.0.9"], capture_output=True, text=True, env=run_env, timeout=90)

    first = run()
    assert first.returncode == 0, first.stdout + first.stderr
    second = run()                                    # again, as after a partial run
    assert second.returncode == 0, second.stdout + second.stderr
    assert (base / "secrets" / "verifiers" / "agent.sha256").read_text() == "a" * 64
    assert (base / "config" / "vision-node.json").read_text() == '{"keep": "me"}'
    assert (base / "config" / "llama.env").read_text() == LLAMA_ENV
    units = home / ".config" / "systemd" / "user"
    assert exec_first_words(units / "llama-vision.service") == ["/usr/bin/env"]   # rendered: literal, absolute
    assert "100.64.0.9" in (base / "config" / "vision-node.json.new").read_text()
    assert (home / ".config" / "systemd" / "user" / "vision-node.service").is_file()
    calls = [c.strip() for c in log.read_text().splitlines() if c.strip()]
    assert calls and set(calls) == {"--user daemon-reload"}


@needs_bash
def test_install_sh_rejects_wildcard_and_junk_bind():
    for bad in ("0.0.0.0", "::", "1.2.3.4; rm -rf x", "host name"):
        r = subprocess.run([bash(), str(KIT / "install.sh"), bad], capture_output=True, text=True, timeout=30)
        assert r.returncode != 0, bad


def _run_stage(tmp_path, install_bytes):
    """Copy the deploy script and a minimal tree to tmp, replace install.sh, run -StageOnly."""
    (tmp_path / "tools").mkdir()
    shutil.copy(PS1, tmp_path / "tools" / PS1.name)
    shutil.copytree(KIT, tmp_path / "deploy" / "vlmpi")
    (tmp_path / "deploy" / "vlmpi" / "install.sh").write_bytes(install_bytes)
    pkg = tmp_path / "services" / "vision-node" / "vision_node"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_bytes(b"x = 1\r\n")
    (tmp_path / "contracts" / "openapi").mkdir(parents=True)
    (tmp_path / "contracts" / "openapi" / "vision-node-internal.yaml").write_bytes(b"openapi: 3.1.0\r\n")
    return subprocess.run([powershell(), "-NoProfile", "-File", str(tmp_path / "tools" / PS1.name), "-StageOnly"],
                          capture_output=True, text=True, timeout=120)


@needs_ps
def test_deploy_script_stops_on_a_syntax_error(tmp_path):
    r = _run_stage(tmp_path, b'#!/usr/bin/env bash\nBIND="${1:?usage: this node\'s ip}"\n')
    assert r.returncode != 0 and "bash -n failed" in (r.stdout + r.stderr)


@needs_ps
def test_deploy_script_converts_crlf_and_passes(tmp_path):
    crlf = (KIT / "install.sh").read_bytes().replace(b"\n", b"\r\n")
    r = _run_stage(tmp_path, crlf)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bash -n: OK" in r.stdout and "deploy/vlmpi/install.sh" in r.stdout


def test_no_unit_or_template_uses_a_variable_as_the_executable():
    """systemd expands neither ${VAR} nor $VAR in the first word of ExecStart: it must be a literal absolute path, a
    %h specifier, or (in a template) the __PLACEHOLDER__ that install.sh renders."""
    found = 0
    for p in list(KIT.glob("*.service")) + list(KIT.glob("*.service.template")):
        for word in exec_first_words(p):
            found += 1
            assert re.fullmatch(r"(/|%h/)[A-Za-z0-9_./+-]+|__[A-Z_]+__", word), f"{p.name}: {word}"
    assert found == 2


@needs_bash
@pytest.mark.parametrize("value", ["", "llama-server", "/no/such/llama-server", "/usr/bin/env;x"])
def test_install_sh_refuses_a_bad_llama_bin(tmp_path, value):
    """Missing, relative, non-executable or odd VLM_BIN stops the install before any unit is written."""
    home = tmp_path / "home"
    (home / "edge-vision" / "config").mkdir(parents=True)
    (home / "edge-vision" / "config" / "llama.env").write_text("VLM_BIN=" + value + "\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "python3").write_text(PYTHON3_STUB, newline="\n")
    (stubs / "systemctl").write_text("#!/bin/bash\nexit 0\n", newline="\n")
    stage = tmp_path / "stage"
    shutil.copytree(KIT, stage / "deploy" / "vlmpi")
    pkg = stage / "services" / "vision-node" / "vision_node"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (stage / "contracts" / "openapi").mkdir(parents=True)
    (stage / "contracts" / "openapi" / "vision-node-internal.yaml").write_text("x: 1\n")
    env = {"HOME": str(home), "PATH": f"{stubs.as_posix()}:/usr/bin:/bin", "SystemRoot": r"C:\Windows"}
    r = subprocess.run([bash(), (stage / "deploy" / "vlmpi" / "install.sh").as_posix(), "100.64.0.9"],
                       capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode != 0 and "VLM_BIN" in r.stdout, (value, r.stdout, r.stderr)
    assert not (home / ".config" / "systemd" / "user" / "llama-vision.service").exists()
