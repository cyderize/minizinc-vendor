#!/usr/bin/env python3
"""Read dependencies.toml and drive the build/publish workflow.

Each (dependency, platform) build becomes an asset on a per-dependency GitHub
Release tagged `<dep>-<version>`. There is no separate compose/packaging step:
the release *is* the artifact, and "already built?" is "does the asset exist?".

Subcommands
-----------
matrix   [--changed BASE] [--changed-dep D ...]   Emit the build matrix as JSON.
env      --dep D --platform P                      Emit `export KEY=VALUE` recipe env.
release-tag --dep D                                Print `<dep>-<version>`.
asset    --dep D --platform P                      Print the release asset filename.
versions                                           Print `<dep> <identity>` per dep.
platforms                                          Print the platform ids.
lock                                               Emit the resolved lockfile (JSON).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for older interpreters
    import tomli as tomllib  # type: ignore

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(HERE, "dependencies.toml")
ASSET_EXT = "tar.gz"

# Install Qt inside a (manylinux) container via aqtinstall for the gist build.
# manylinux_2_34 glibc 2.34, which the official Qt 6 Linux binaries require.
QT_CONTAINER_SETUP = """\
dnf -y install libxcb-devel libxkbcommon-devel mesa-libGL-devel fontconfig-devel freetype-devel libX11-devel 2>/dev/null || true
# The system python3 (AlmaLinux 8 => 3.6) is too old for aqtinstall; use a bundled manylinux python.
PY=$(ls -d /opt/python/cp311-cp311/bin/python /opt/python/cp312-cp312/bin/python 2>/dev/null | head -1)
"$PY" -m pip install --quiet aqtinstall
# Qt has distinct repository hosts for x86_64 and ARM64 Linux. The x86_64 kit
# was renamed from gcc_64 to linux_gcc_64; resolve that name, while ARM64 uses
# linux_gcc_arm64. Take the install dir aqt actually created.
QT_ARCH=$("$PY" -m aqt list-qt {host} desktop --arch {ver} | tr ' ' '\\n' | grep -E '{arch_pattern}' | head -1)
test -n "$QT_ARCH"
"$PY" -m aqt install-qt {host} desktop {ver} "$QT_ARCH" --outputdir /opt/qt
QT_DIR=$(ls -d /opt/qt/{ver}/*/ | head -1)
QT_DIR=${{QT_DIR%/}}
export PATH="$QT_DIR/bin:$PATH"
export CMAKE_PREFIX_PATH="$QT_DIR:${{CMAKE_PREFIX_PATH:-}}\""""

# bazelisk release asset per platform (empty => Alpine/musl uses `apk add bazel8`).
BAZELISK_ASSET = {
    "linux": "linux-amd64",
    "linux-arm64": "linux-arm64",
    "musl": "",
    "musl-arm64": "",
    "osx": "darwin-arm64",
    "osx-intel": "darwin-amd64",
    "win64": "windows-amd64",
    "win64-arm": "windows-arm64",
}


def load() -> dict:
    with open(MANIFEST, "rb") as f:
        return tomllib.load(f)


def resolve_dep(m: dict, name: str) -> dict:
    """Return a dep entry with `source_of` inheritance applied (gecode_gist shares
    gecode's version/commit)."""
    d = dict(m["deps"][name])
    if "source_of" in d:
        parent = m["deps"][d["source_of"]]
        for k in ("version", "commit", "rebuild"):
            if k in parent and k not in d:
                d[k] = parent[k]
    return d


def dep_version_label(d: dict) -> str:
    """The immutable identity of a dep build: its tag, or its commit, plus the
    `rebuild` counter if the same source has been republished (new flags, new
    toolchain). Recipes still get the bare `version`/`commit` via DEP_VERSION."""
    base = d["version"] if "version" in d else d["commit"]
    return f"{base}-rebuild{d['rebuild']}" if "rebuild" in d else base


def release_tag(name: str, d: dict) -> str:
    return f"{name}-{dep_version_label(d)}"


def asset_name(m: dict, name: str, d: dict, platform: str) -> str:
    triple = m["platforms"][platform]["triple"]
    return f"{name}-{dep_version_label(d)}-{triple}.{ASSET_EXT}"


def cell_setup(m: dict, platform: str, qt_in_container: bool) -> str:
    """System-dependency install commands run (as POSIX sh) before the recipe."""
    setup = m["platforms"][platform].get("setup", "")
    if qt_in_container:
        ver = m["toolchain"]["qt"]["version"]
        qt_host, qt_arch_pattern = (
            ("linux_arm64", "^linux_gcc_arm64$")
            if platform == "linux-arm64"
            else ("linux", "^(linux_)?gcc_64$")
        )
        setup = (setup + "\n" if setup else "") + QT_CONTAINER_SETUP.format(
            ver=ver, host=qt_host, arch_pattern=qt_arch_pattern
        )
    return setup


def build_cell(m: dict, name: str, platform: str) -> dict:
    d = resolve_dep(m, name)
    plat = m["platforms"][platform]
    container = plat.get("container") or ""
    uses = d.get("uses", [])
    qt = "qt" in uses
    qt_in_container = qt and container != ""
    env_b64 = base64.b64encode("\n".join(env_kv(m, name, platform)).encode()).decode()
    setup_b64 = base64.b64encode(cell_setup(m, platform, qt_in_container).encode()).decode()
    return {
        "dep": name,
        "platform": platform,
        "triple": plat["triple"],
        "runner": plat["runner"],
        # Empty string means "no container" — the workflow maps "" -> native.
        "container": container,
        "recipe": d["recipe"],
        "gist": "1" if d.get("gist") else "",
        "artifact_dir": d["artifact_dir"],
        "release_tag": release_tag(name, d),
        "asset_name": asset_name(m, name, d, platform),
        # Qt on the native (osx/win64) gist runners is installed via install-qt-action;
        # gist-in-container (linux) gets Qt via aqtinstall in the setup script.
        "needs_qt": qt and container == "",
        "qt_version": m["toolchain"]["qt"]["version"] if qt else "",
        "cache_kind": "bazel" if "bazel" in uses else "ccache",
        "env_b64": env_b64,
        "setup_b64": setup_b64,
        "label": f"{name}:{platform}",
    }


def all_cells(m: dict) -> list[dict]:
    cells = []
    for name in m["deps"]:
        for platform in resolve_dep(m, name)["platforms"]:
            cells.append(build_cell(m, name, platform))
    return cells


def env_kv(m: dict, name: str, platform: str) -> list[str]:
    """Recipe environment as raw `KEY=VALUE` lines (for $GITHUB_ENV / --env-file)."""
    d = resolve_dep(m, name)
    plat = m["platforms"][platform]
    out: list[str] = []

    def put(k, v):
        out.append(f"{k}={v}")

    put("MZNARCH", plat["mznarch"])
    if platform != "wasm":
        put("CMAKEARCH", plat.get("cmake_generator", "Ninja"))
    if "version" in d:
        put("DEP_VERSION", d["version"])
    if "commit" in d:
        put("DEP_COMMIT", d["commit"])

    uses = d.get("uses", [])
    if "coinbrew" in uses:
        put("COINBREW_COMMIT", m["toolchain"]["coinbrew"]["commit"])
    if "rules_pkg" in uses:
        put("RULES_PKG_VERSION", m["toolchain"]["rules_pkg"]["version"])
    if "bazel" in uses:
        put("BAZEL_VERSION", m["toolchain"]["bazel"]["version"])
        put("BAZELISK_VERSION", m["toolchain"]["bazelisk"]["version"])
        put("BAZELISK_ASSET", BAZELISK_ASSET.get(platform, ""))

    # Platform-wide first, so a dependency can still override it.
    for k, v in plat.get("env", {}).items():
        put(k, v)
    for k, v in d.get("env", {}).get(platform, {}).items():
        put(k, v)
    return out


def env_lines(m: dict, name: str, platform: str) -> list[str]:
    """Recipe environment as shell `export KEY=VALUE` lines (for local `eval`)."""
    return [f"export {kv.split('=', 1)[0]}={shlex.quote(kv.split('=', 1)[1])}"
            for kv in env_kv(m, name, platform)]


def _identity_of(entry: dict) -> str | None:
    return entry.get("version") or entry.get("commit")


def changed_deps(base_ref: str) -> set[str] | None:
    """Deps affected by the diff vs base_ref. None => cannot narrow (build all)."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=HERE, capture_output=True, text=True, check=True,
        ).stdout.split()
    except Exception:
        return None

    m = load()
    changed: set[str] = set()

    # A change to the tooling that drives every build => rebuild everything.
    if any(f.startswith("scripts/") or f.startswith(".github/") for f in diff):
        return None

    # A changed recipe or overlay file => that dependency.
    for name in m["deps"]:
        d = resolve_dep(m, name)
        overlay = d.get("overlay")
        for f in diff:
            if f == d["recipe"] or (overlay and f.startswith(overlay + "/")):
                changed.add(name)

    # A manifest edit: diff pins against the base to find exactly what moved.
    if "dependencies.toml" in diff:
        try:
            base_raw = subprocess.run(
                ["git", "show", f"{base_ref}:dependencies.toml"],
                cwd=HERE, capture_output=True, check=True,
            ).stdout
            base = tomllib.loads(base_raw.decode())
        except Exception:
            return None

        for name in m["deps"]:
            b = base.get("deps", {}).get(name)
            head_id = _identity_of(resolve_dep(m, name))
            base_id = _identity_of(resolve_dep(base, name)) if b is not None else None
            if head_id != base_id:
                changed.add(name)

        for tname, tentry in m.get("toolchain", {}).items():
            b = base.get("toolchain", {}).get(tname, {})
            if _identity_of(tentry) != _identity_of(b):
                for name, entry in m["deps"].items():
                    if tname in entry.get("uses", []):
                        changed.add(name)

        if m.get("platforms") != base.get("platforms"):
            return None

    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("matrix")
    p.add_argument("--changed")
    p.add_argument("--changed-dep", action="append", default=[])

    p = sub.add_parser("env")
    p.add_argument("--dep", required=True)
    p.add_argument("--platform", required=True)

    p = sub.add_parser("asset")
    p.add_argument("--dep", required=True)
    p.add_argument("--platform", required=True)

    p = sub.add_parser("release-tag")
    p.add_argument("--dep", required=True)

    sub.add_parser("versions")
    sub.add_parser("platforms")
    sub.add_parser("lock")

    args = ap.parse_args()
    m = load()

    if args.cmd == "matrix":
        cells = all_cells(m)
        selected = set(args.changed_dep)
        if args.changed and not selected:
            cd = changed_deps(args.changed)
            if cd is not None:
                selected = cd
        if selected:
            cells = [c for c in cells if c["dep"] in selected]
        print(json.dumps(cells))
    elif args.cmd == "env":
        print("\n".join(env_lines(m, args.dep, args.platform)))
    elif args.cmd == "asset":
        print(asset_name(m, args.dep, resolve_dep(m, args.dep), args.platform))
    elif args.cmd == "release-tag":
        print(release_tag(args.dep, resolve_dep(m, args.dep)))
    elif args.cmd == "versions":
        for name in m["deps"]:
            print(f"{name} {dep_version_label(resolve_dep(m, name))}")
        # Not a dep, but a downstream pin all the same: libminizinc must link the
        # wasm archives with the emscripten that built them. Reported here so the
        # existing reconcile/bump path keeps it in sync.
        print(f"emsdk {m['platforms']['wasm']['container'].rsplit(':', 1)[-1]}")
    elif args.cmd == "platforms":
        print("\n".join(m["platforms"].keys()))
    elif args.cmd == "lock":
        lock = {"schema": m.get("schema"), "toolchain": m["toolchain"], "deps": {}}
        for name in m["deps"]:
            d = resolve_dep(m, name)
            lock["deps"][name] = {
                "identity": dep_version_label(d),
                "version": d.get("version"),
                "commit": d.get("commit"),
                "release_tag": release_tag(name, d),
                "assets": {p: asset_name(m, name, d, p) for p in d["platforms"]},
            }
        print(json.dumps(lock, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
