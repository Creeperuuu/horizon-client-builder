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
**Actions: read and write** and **Contents: read and write** (the site uploads the selected `.hnmod` archives to a temporary branch). The page keeps it in the tab's memory and sends it nowhere but api.github.com.

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

Each build is a pre-release named `horizon-client-<short>-<profile>` (plus `-m<digest>` with mods) with `horizon-client.exe` and
`SHA256SUMS`. The newest 25 are kept.


## Native `.hnmod` bundles

Select one or more `.hnmod` files in the site before starting the build. The site uploads them through GitHub's
REST Contents API to a temporary `hnmod-upload-*` branch (public, like the rest of the repository, and deleted by
the daily cleanup), then starts the workflow with that branch. A build without mods is a clean client.

What `scripts/prepare_mods.py` does follows the Horizon launcher's native build (`docs/native-mods.md`):

1. **Checks each archive** by the rules of Horizon's own `Bundle::read`: no links or `..`, only Rust, TOML and
   `data/` files under `mixins/<id>/`, at most 512 entries, ids of `a-z`, `0-9` and `_`, and the manifest and the
   mixin file must agree on id, version and Horizon commit. Nothing runs until the build.
2. **Chooses the Horizon commit** from the mods' pins (`[compatibility] horizon_revision`), newest first, and uses the
   first one every mod applies on. Mods that only name a hook points' version (`hooks`) build on the revision you
   asked for when its `NATIVE_HOOKS_API` is new enough. Every mod is moved to the commit that is built.
3. **Installs `horizon-mixins`** at the newest commit among the mods' `mixins` pins and the one the chosen Horizon
   commit names (`MIXINS_REV`), and runs `apply`.
4. Compiles the staged source. Vendored Opus that assumes GCC is made MSVC-safe (`scripts/fix_msvc_opus.py`; it does
   nothing for a clean client).

The release says which commit was really built (the mods' pins can differ from the revision asked for) and which
mods are in it. A modded build never replaces the clean one: the tag carries a digest of the mods' files
(`horizon-client-<short>-<profile>-m<digest>`).

**Not supported yet**, and refused by name instead of built wrongly: mods with an `[api]` trait crate or
`trait_method` injections, and `[[post_build]]` steps (the launcher runs those inside an installed version folder,
which a lone exe doesn't have). Native mods aren't sandboxed: only build mods whose source you trust.

Limits: 10 mods per build, 40 MiB per archive, 200 MiB total. The fine-grained token must have Actions and Contents
read/write access to this repository.

## Caching

Builds get faster once the caches are warm. The run summary says whether the dependency cache hit.

- **Dependencies** (`Swatinem/rust-cache`): Horizon's compiled crates.io dependencies and the downloads, one cache per
  profile. It is restored *after* the mods are staged (so its key is the `Cargo.lock` that is built) into a `target/`
  folder beside the source, not inside it: staging replaces the source tree, and that used to delete a restored
  `target/`, so every modded build compiled everything cold. A new commit rebuilds only Horizon's own crates.
- **horizon-mixins** (`actions/cache`): the tool takes minutes to compile and only changes with its commit. It is
  installed with `cargo install --locked --root` (as the launcher does, which also avoids the second Rust that the
  checkout's `rust-toolchain.toml` would download) and saved the moment it is built, even if a later step fails.
- Horizon's own crates are never reused across commits and every source file gets a fresh time, so a restored cache
  can't link a stale crate (Horizon's #10421).

## Storage cleanup

`.github/workflows/cleanup.yml` runs daily and deletes workflow runs and releases older than 24 hours. Build artifacts already have a one-day retention period. The cleanup can also be started manually from Actions.

## Playing a build

The exe alone can't start: it needs a version folder's game files. In the Horizon launcher, import it as a
build (Versions page): it is installed beside your releases and plays with their files. Builds are unsigned.
