# horizon-client builder

Builds **`horizon-client.exe`** (Windows x64) from [horizon-rust/horizon](https://github.com/horizon-rust/horizon)
on GitHub Actions, at the revision you pick, and lists every finished build on a GitHub Pages site with a
download button.

**Site:** https://creeperuuu.github.io/horizon-client-builder/

## Using it

Open the site, pick a revision and a profile, optionally select one or more `.hnmod` files, paste a fine-grained token and press **Start build**. When it finishes
the build appears in the list; downloading needs no token. You can also start one from the Actions tab or
the CLI:

```
gh workflow run build.yml -f revision=nightly
gh workflow run build.yml -f revision=2026-11-05-f319701 -f profile=fast
```

The token is only for *starting* a build. Use a fine-grained token limited to this repository with
**Actions: read and write**, and **Contents: read and write** (the site creates a temporary draft release to upload the selected `.hnmod` archives). The page keeps it in the tab's memory and sends it nowhere but api.github.com.

## Revisions

The same pinning Horizon's own launcher and CI use. `scripts/resolve_revision.py` turns the revision into
one full commit, and the build downloads exactly that commit.

| Revision | Means |
| --- | --- |
| `nightly` | the newest `nightly-source/<date>-<short>` tag |
| `2026-11-05-f319701` | that Nightly. Horizon keeps these tags for good, so old Nightlies stay buildable |
| `v2026.10.23` | a release tag |
| `main`, any branch | the branch's tip when the build starts |
| a commit hash (7-40 chars) | exactly that commit |

A Nightly or release tag is stamped into the build the way Horizon's CI does it (`HORIZON_VERSION`,
`HORIZON_NIGHTLY`: the game then says which build it is). A branch or a commit builds as a build from
source, like a local one.

## How it builds

Copied from Horizon's `game-build.yml` and the launcher's `native_mods.rs`:

- source fetched by commit from `codeload.github.com`, links left out, the commit written to
  `.horizon-revision`, every file given the current time so a restored Cargo cache never links a stale crate
  (Horizon's #10421). See `scripts/fetch_source.py`.
- `cargo build --locked --profile <release|fast> -p horizon-client --bin horizon-client`, with
  `RUSTFLAGS=-C target-feature=+crt-static` so no Visual C++ redistributable is needed.
- `release` is what Horizon ships (thin LTO, slow); `fast` is the launcher's default for native builds
  (no LTO, much quicker).
- the result is checked with Horizon's own `tools/check_imports.py` and by starting it.
- the `build` job has read-only access and no secrets (it compiles someone's commit); only `publish` can write,
  and it never runs what was built.

Each build is a pre-release named `horizon-client-<short>-<profile>` with `horizon-client.exe` and
`SHA256SUMS`. The newest 25 are kept.


## Native `.hnmod` bundles

Select one or more `.hnmod` files in the site before starting the build. The site uploads them through GitHub's REST Contents API to a temporary `hnmod-upload-*` branch, then dispatches the workflow with that branch name. This avoids browser CORS failures on GitHub's separate release-asset upload host. The Windows runner fetches the temporary branch and validates the archives, extracts each `horizon-native.json` and `mixins/<id>/horizon-mixin.toml`, and applies the bundles with Horizon's `horizon-mixins` CLI before compiling `horizon-client.exe`.

The builder checks archive paths and sizes, rejects duplicate mod IDs and inconsistent Horizon pins, and stages mods together. It selects a Horizon revision from the mods' pinned commits (newest first), uses the newest pinned `horizon-mixins` commit, and reports a failure if the bundles cannot be applied together. A native mod is executable native code, not sandboxed. Only upload mods you trust.

Limits: 10 mods per build, 40 MiB per archive, 200 MiB total. The fine-grained token must have Actions and Contents read/write access to this repository. Temporary upload branches are deleted by the daily cleanup workflow after 24 hours.

## Storage cleanup

`.github/workflows/cleanup.yml` runs daily and deletes workflow runs and releases older than 24 hours. Build artifacts already have a one-day retention period. The cleanup can also be started manually from Actions.

## Playing a build

The exe alone can't start: it needs a version folder's game files. In the Horizon launcher, import it as a
build (Versions page): it is installed beside your releases and plays with their files. Builds are unsigned.
