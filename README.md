# Horizon Client Builder

A static web page + GitHub Actions pipeline that compiles the
[Horizon](https://github.com/horizon-rust/horizon) Minecraft client for desktop
platforms — with your own source-based **.hnmod** native mods compiled straight
into the executable.

**The site is live at:** `https://creeperuuu.github.io/horizon-client-builder/`

## How it works

```
browser (your token, memory only)
  │  1. uploads .hnmod files to a temporary branch builder-upload-…
  │  2. dispatches .github/workflows/build.yml
  ▼
GitHub Actions runner
  │  3. checks out Horizon (branch / tag / commit you picked)
  │  4. validates + unpacks the mods          (scripts/prepare_mods.py)
  │  5. probes each mod's pinned Horizon commit (newest first), then the
  │     requested one, and builds at the first revision where all mods
  │     still apply                          (scripts/probe_revisions.sh)
  │  6. plans + applies all mods, compiles the client
  │  7. uploads the horizon-client-<platform> artifact
  │  8. deletes the temporary upload branch
  ▼
browser
  │  9. polls the run, downloads the artifact zip
  ▼
you (~1 hour until the scheduled sweep deletes the artifact)
```

No Cloudflare Worker, no stored secrets, no CAPTCHA: the page is a static
GitHub Pages site, and every API call is made **from your browser** with the
token you paste in. The token is held in the tab's memory only — it is never
saved to storage, never logged, and sent nowhere but `api.github.com`. Closing
the tab forgets it.

## Using the builder

1. **Token** — create a fine-grained PAT for the target repository with
   **Actions: read and write**, **Contents: read and write**, **Metadata:
   read** (or a classic PAT with `repo`). Paste it into the site and hit *Check*.
2. **Repository** — defaults to this repo. To build with your own Actions
   quota, fork the repo, enable Actions on the fork, and put `your-name/horizon-client-builder`
   in the field.
3. **Settings** — Horizon source (branch/tag/commit, default `main`), platform,
   and profile (`fast` ≈ dev build, `release` optimized).
4. **Mods** — drop up to 10 `.hnmod` files (40 MiB each), or none for a clean client.
5. **Build** — the page shows live progress and hands you a download link when
   the artifact exists.

### Platforms

| Site option | Runner | Executable |
|---|---|---|
| Windows x64 | `windows-latest` | `horizon-client.exe` |
| Linux x64 | `ubuntu-latest` | `horizon-client` |
| Linux ARM64 | `ubuntu-24.04-arm` | `horizon-client` |
| macOS Apple Silicon | `macos-14` | `horizon-client` |

First build per platform is slow (cold cargo cache, ~1–2 h). Repeat builds with
the same Horizon commit reuse the Rust/registry caches and are much quicker.

## Retention

A scheduled sweep (`.github/workflows/cleanup.yml`, hourly, uses only the
built-in `GITHUB_TOKEN`) deletes:

| What | After | Notes |
|---|---|---|
| Built clients (artifacts) | **~1 hour** | download promptly |
| Completed build/cleanup run history | **24 hours** | keeps the Actions tab clean |
| Abandoned `builder-upload-*` branches | **6 hours** | the build deletes its own branch immediately |

Tune the hours via the `ARTIFACT_MAX_AGE_HOURS`, `RUN_MAX_AGE_HOURS` and
`BRANCH_MAX_AGE_HOURS` env vars at the top of `cleanup.yml`. Website deploy
history is left untouched.

## The revision workaround

Native mods pin an exact Horizon commit in their
`[compatibility].horizon_revision`, and a mod only applies cleanly to the
Horizon source it was written against — on newer commits its hook anchors may
have been renamed or restructured (a free function turned into a method, and
so on). Simply forcing every pin to the requested commit, like the first
version of this builder did, therefore produced builds that failed in the
mixin `plan` step with `E_TARGET_NOT_FOUND`.

The builder now **probes** instead (`.github/workflows/build.yml` +
`scripts/probe_revisions.sh`):

1. collect the distinct pins of all selected mods,
2. try each pinned commit, newest first — the pins are the commits the mods
   were actually written for, so they are the strongest compatibility signal,
3. then the Horizon commit you requested, as the last candidate,
4. at each candidate: check out Horizon there, neutralize the mods' pins and
   run the mixin tooling's `plan`,
5. the first candidate where *every* mod's source anchors resolve becomes the
   build revision; the client is compiled against it.

So mods written against different Horizon revisions build together with no
settings, and they are built against the revision they were written for. With
no pinned mods the requested ref is used directly; with pinned mods it is
tried last, so it still wins whenever the mods also apply there — but a pin
that only works on its own commit takes precedence. Genuinely incompatible
mod sets fail the probe with a per-candidate report (run summary) listing
exactly which anchors were missing where.

One Horizon source tree is still used per build - mods whose anchors only
exist on mutually exclusive commits cannot be combined and must be built in
separate runs.

## The Windows toolchain shim

Mods also vendor C code, and a mod built on Linux/macOS usually compiles it
with GCC/Clang assumptions. The common case is opus's `-DVAR_ARRAYS` mode,
which implements its stack allocations as C99 variable-length arrays - cl.exe
cannot compile those, and the first Windows mod build failed with exactly
that (`C2057`/`C2466`/`C2133` in `opus_decoder.c`).

Before cargo builds a Windows client, `scripts/patch_native_compat.py`
rewrites the staged native crates' build scripts from those definitions to
the portable equivalents opus itself uses on Windows (`VAR_ARRAYS` ->
`USE_ALLOCA`, i.e. stack allocas - same semantics, supported by MSVC, GCC
and Clang alike). The patch is source-level, idempotent, limited to
`game-stage/native-crates/**/build.rs`, and every change is reported in the
build log, the run summary and `build-info.txt`. C sources are never
touched, and non-Windows builds are left exactly as the mod ships them.

## Limits & safety

- ≤ 10 `.hnmod` files per build, ≤ 40 MiB each (200 MiB unpacked) — enforced by
  `scripts/prepare_mods.py`.
- Uploaded mods sit on a **temporary public branch** while the build runs (the
  branch is deleted when the build finishes). Don't upload private mods to a
  public repo.
- Native mods are unsandboxed Rust compiled into the executable — only build
  mods you trust.
- Duplicate mod IDs, conflicting hooks and Rust compile errors fail the build.

## Repository setup (one-time)

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
2. Push to `main` (or run *Deploy builder website* manually) — the site deploys
   to Pages automatically.
3. That's it. No secrets to configure: every workflow uses `GITHUB_TOKEN`.
4. (Optional) delete any legacy `repotoken` Actions secret — the new design
   stores no tokens anywhere.

## Files

```
web/index.html                     the whole builder page (no build step, no deps)
scripts/prepare_mods.py            .hnmod validation/unpacking, pin listing, revision re-pinning
scripts/probe_revisions.sh         mixed-revision workaround: probe pins, pick the working revision
scripts/patch_native_compat.py     Windows shim: VAR_ARRAYS -> USE_ALLOCA in mod crate build scripts
scripts/materialize-symlinks.sh    tracked-symlink materialization (reused after every probe switch)
.github/workflows/build.yml        the dispatcher build (4 platforms, fast/release)
.github/workflows/cleanup.yml      hourly retention sweep (artifacts / runs / branches)
.github/workflows/deploy-pages.yml publishes web/ to GitHub Pages
```
