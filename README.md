# horizon-client builder

Builds **`horizon-client.exe`** (Windows x64) from [horizon-rust/horizon](https://github.com/horizon-rust/horizon)
on GitHub Actions, at the revision you pick and with your native mods (`.hnmod`) compiled in, and lists every
finished build on a GitHub Pages site with a download button.

**Site:** https://creeperuuu.github.io/horizon-client-builder/

## Using it

Open the site, pick a revision and a profile, paste a token and press **Start build**. When it finishes
the build appears in the list; downloading needs no token. You can also start one from the Actions tab or
the CLI:

```
gh workflow run build.yml -f revision=nightly
gh workflow run build.yml -f revision=2026-11-05-f319701 -f profile=fast
```

The token is only for *starting* a build and for adding or removing mods. Use a fine-grained token limited to
this repository with **Actions: read and write** and **Contents: read and write**. The page keeps it in the
tab's memory and sends it nowhere but api.github.com.

## Mods

Put native mods in [`mods/`](mods) (the site's **Mods** panel uploads them there): `.hnmod` files, an unpacked
`.hnmod`, or a bundle folder with `horizon-mixin.toml`. A build includes all of them unless you name some
(`mods=a.hnmod,my_mod`) or none (`mods=none`, a clean client).

It follows the launcher's native build (`docs/native-mods.md` in Horizon):

1. each `.hnmod` is checked by the rules of Horizon's own `Bundle::read` (no links or `..`, only Rust, TOML and
   `data/`, size limits, manifest and mixin agree). Nothing in it runs until the build.
2. **The commit to build on** comes from the mods' pins (`[compatibility] horizon_revision`), as in the launcher:

   | Mods | Builds on |
   | --- | --- |
   | none pin a commit | the revision you asked for |
   | all pin one commit | that commit, even if you asked for another |
   | pin different commits | the newest of them, only with `allow_mismatch`; else the build stops and says who pins what |
   | name a hook points' version (`hooks`) | a commit whose `NATIVE_HOOKS_API` is that or later and has the named hook points |
   | `pins=ignore` | the revision you asked for, every mod moved to it ("Rebuild on Horizon") |

3. `horizon-mixins` is installed at the commit the mods name (else the `MIXINS_REV` the launcher of that Horizon
   commit uses), `apply` stages the modded source, and that is compiled with `--features horizon-client/mods`
   while the revision has it. Rust is the launcher's pinned version at that commit.
4. Vendored C that assumes GCC is made MSVC-safe (`scripts/patch_native_compat.py`); the mods' crates are fetched
   `--locked` against Horizon's `Cargo.lock`.

A broken mod stops only builds that include it. **Not supported yet**, and named instead of built wrongly: mods
with an `[api]` trait crate or `trait_method` injections, and `[[post_build]]` steps (the launcher runs those in an
installed version folder, which a lone exe doesn't have). Native mods aren't sandboxed: build only source you trust.

## Caching

- **Dependencies** (`Swatinem/rust-cache`): Horizon's compiled crates.io dependencies and downloads, restored
  before anything else in the job into one target folder shared by clean and modded builds. A new commit rebuilds only
  Horizon's own crates; the run summary says whether the cache hit. One cache per profile (`release`, `fast`).
- **horizon-mixins** (`actions/cache`): the tool takes minutes to compile and only changes with its commit. It is saved
  the moment it is built, so a build that fails later (a mod that doesn't apply) keeps it.
- Horizon's own crates are never cached across commits and every source file gets a fresh time, so a restored cache
  can't link a stale crate (Horizon's #10421).

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

## Playing a build

The exe alone can't start: it needs a version folder's game files. In the Horizon launcher, import it as a
build (Versions page): it is installed beside your releases and plays with their files. Builds are unsigned.
