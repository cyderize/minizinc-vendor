# minizinc-vendor

Reproducible builds of the third-party dependencies that ship with MiniZinc —
CBC, HiGHS, OR-Tools CP-SAT, Gecode, Gecode+Gist, Chuffed, and the OpenSSL
runtime for the Windows installer — for every supported platform.

Every version is pinned in one manifest, every build is published individually,
and upstream updates arrive as pull requests.

## Model

Each `(dependency, platform)` build is one asset on a per-dependency GitHub
Release tagged `<dep>-<version>`:

```
Release  gecode-6.4.0
  asset    gecode-6.4.0-x86_64-linux-gnu.tar.gz
  asset    gecode-6.4.0-aarch64-apple-darwin.tar.gz
  ...
```

There is no compose step and no separate artifact registry: the release *is* the
artifact, and "already built?" is "does that asset exist?". Bumping one
dependency therefore rebuilds only that dependency — every other asset already
exists and is skipped. Old versions stay published, so any pinned build can be
re-downloaded without rebuilding.

Consumers (libminizinc, and through it the IDE) keep a `vendor.lock` of per-
dependency versions and download the asset for their own triple.

## Layout

- **`dependencies.toml`** — the single source of truth: every solver and
  toolchain pin, and every platform's runner, base image, triple and `setup`
  command. A dep may also carry `rebuild = <n>` to republish the same source
  under new flags or a new toolchain.
- **`recipes/*.sh`** — one per dependency. Versions arrive in the environment
  (injected from the manifest), never hard-coded. Each builds into
  `$BUILD_ROOT/vendor/<artifact_dir>`.
- **`resources/`** — overlay files applied to an upstream source tree, currently
  the OR-Tools packaging target.
- **`scripts/manifest.py`** — expands the `(dependency × platform)` matrix and
  computes each cell's release tag, asset name and recipe environment.
- **`scripts/prune_built.py`** — drops matrix cells whose asset already exists.
- **`scripts/update_check.py`** — resolves the newest upstream version of each
  tracked pin, and rewrites one pin in place.

Base images are stock public ones — `manylinux_2_34` (glibc 2.34 floor) for
glibc-linux, `alpine` for musl, `emscripten/emsdk` for wasm — with build deps
installed at job time via the platform's `setup`, so there is no bespoke build
image to maintain. Qt for `gecode_gist` is fetched on demand: `aqtinstall`
inside the linux container, `install-qt-action` on the native runners.

### Workflows

| Workflow | Trigger | Does |
|---|---|---|
| `build.yml` | reusable / dispatch | matrix build; build and optionally publish each missing asset |
| `publish.yml` | push to `main` / dispatch | publish all missing assets, then open a bump PR per dependency in libminizinc |
| `update-bot.yml` | weekly / dispatch | one PR per outdated pin |
| `pr-validate.yml` | PR touching the manifest, a recipe or a resource | rebuild just the affected dependencies, publishing nothing |

The two bot workflows mint installation tokens from a GitHub App via the org
secrets `MINIZINC_BOT_APP_ID` and `MINIZINC_BOT_APP_KEY`; `GITHUB_TOKEN` cannot
write cross-repo, and PRs it creates do not trigger CI. Publishing this repo's
own releases uses `GITHUB_TOKEN`.

### Platforms

`x86_64-linux-gnu`, `aarch64-linux-gnu`, `x86_64-linux-musl`,
`aarch64-linux-musl`, `aarch64-apple-darwin`, `x86_64-apple-darwin`,
`x86_64-windows`, `aarch64-windows`, `wasm32-emscripten`.

Not every dependency covers every platform; see each `platforms` list in the
manifest. To move one platform to a self-hosted runner, change its `runner`
field — no workflow edit needed.

## Local use

```sh
pip install tomli                              # only on Python < 3.11
python3 scripts/manifest.py versions           # dep -> pinned version
python3 scripts/manifest.py release-tag --dep gecode
python3 scripts/manifest.py asset --dep cbc --platform win64
python3 scripts/manifest.py matrix | python3 -m json.tool
python3 scripts/manifest.py env --dep or-tools --platform linux
python3 scripts/manifest.py lock               # resolved lockfile
python3 scripts/update_check.py check          # what is outdated upstream
```
