"""Shell-Unit-Tests für docker-entrypoint.sh (root/non-root, alle CMD-Varianten)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"

_FAKE_SU = r"""#!/usr/bin/env bash
set -euo pipefail
{
  printf 'argv:'
  for a in "$@"; do printf ' [%s]' "$a"; done
  printf '\n'
} >> "$SU_LOG_FILE"
shell=""
cmd=""
while (($#)); do
  case "$1" in
    -s) shell="$2"; shift 2 ;;
    -c) cmd="$2"; shift 2 ;;
    *) break ;;
  esac
done
user="$1"; shift
if [[ "${1:-}" == "--" ]]; then shift; fi
cmd0="${1:-}"; shift
exec "$shell" -c "$cmd" "$cmd0" "$@"
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    su_log = tmp_path / "su.log"
    rr_log = tmp_path / "rr.log"
    (bindir / "id").write_text("#!/bin/sh\nprintf '%s\\n' \"${FAKE_UID:-1000}\"\n")
    (bindir / "chown").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "su").write_text(_FAKE_SU)
    (bindir / "radio-ripper").write_text(
        "#!/bin/sh\n"
        "{\n"
        '  printf \'%s\' "$(basename "$0")"\n'
        '  for a in "$@"; do printf \' %s\' "$a"; done\n'
        "  printf '\\n'\n"
        '} >> "$RR_LOG_FILE"\n'
    )
    for name in ("id", "chown", "su", "radio-ripper"):
        (bindir / name).chmod(0o755)
    return {"bin": bindir, "su_log": su_log, "rr_log": rr_log}


def _brackets(args: list[str]) -> str:
    return " ".join(f"[{item}]" for item in args)


def _run_entrypoint(
    sandbox: dict[str, Path], cfg: Path, uid: int, cmd_args: list[str]
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{sandbox['bin']}:/usr/bin:/bin",
        "FAKE_UID": str(uid),
        "SU_LOG_FILE": str(sandbox["su_log"]),
        "RR_LOG_FILE": str(sandbox["rr_log"]),
        "RADIO_RIPPER_CONFIG": str(cfg),
    }
    return subprocess.run(  # noqa: S603 - trusted args from the test matrix
        [shutil.which("sh") or "/bin/sh", str(ENTRYPOINT), *cmd_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _logs(sandbox: dict[str, Path]) -> tuple[str, str]:
    su_log = sandbox["su_log"].read_text().strip() if sandbox["su_log"].exists() else ""
    rr_log = sandbox["rr_log"].read_text().strip() if sandbox["rr_log"].exists() else ""
    return su_log, rr_log


_CASES: list[tuple[int, bool, list[str], list[str]]] = [
    # (uid, config_present, cmd_args, expected_radio_ripper_argv)
    (1000, True, [], ["radio-ripper", "--config", "CFG"]),
    (1000, True, ["radio-ripper"], ["radio-ripper", "--config", "CFG"]),
    (1000, True, ["--config", "/custom.json"], ["radio-ripper", "--config", "/custom.json"]),
    (1000, True, ["radio-ripper", "--config", "/custom.json"], ["radio-ripper", "--config", "/custom.json"]),
    (1000, True, ["--config=/custom.json"], ["radio-ripper", "--config=/custom.json"]),
    (1000, True, ["-c", "/custom.json"], ["radio-ripper", "-c", "/custom.json"]),
    (1000, True, ["--log-level", "DEBUG"], ["radio-ripper", "--config", "CFG", "--log-level", "DEBUG"]),
    (1000, False, [], ["radio-ripper"]),
    (1000, False, ["radio-ripper"], ["radio-ripper"]),
    (1000, False, ["--config", "/custom.json"], ["radio-ripper", "--config", "/custom.json"]),
    (1000, False, ["--log-level", "DEBUG"], ["radio-ripper", "--log-level", "DEBUG"]),
    (0, True, [], ["radio-ripper", "--config", "CFG"]),
    (0, True, ["radio-ripper"], ["radio-ripper", "--config", "CFG"]),
    (0, True, ["--config", "/custom.json"], ["radio-ripper", "--config", "/custom.json"]),
    (0, True, ["radio-ripper", "--config", "/custom.json"], ["radio-ripper", "--config", "/custom.json"]),
    (0, False, [], ["radio-ripper"]),
    (0, False, ["radio-ripper"], ["radio-ripper"]),
]


@pytest.mark.parametrize(("uid", "config_present", "cmd_args", "expected"), _CASES)
def test_entrypoint_builds_command(
    sandbox: dict[str, Path], tmp_path: Path, uid: int, config_present: bool, cmd_args: list[str], expected: list[str]
) -> None:
    cfg = tmp_path / "config.json"
    if config_present:
        cfg.write_text("{}")
    expected = [str(cfg) if item == "CFG" else item for item in expected]

    proc = _run_entrypoint(sandbox, cfg, uid, cmd_args)

    assert proc.returncode == 0, proc.stderr
    su_log, rr_log = _logs(sandbox)
    assert rr_log == " ".join(expected)
    if uid == 0:
        assert f"[ripper] [--] {_brackets(expected)}" in su_log
    else:
        assert su_log == ""
