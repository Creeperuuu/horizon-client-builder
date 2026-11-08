# mods

Put your native mods here and they are compiled into `horizon-client.exe`:

- `*.hnmod` files, or
- an unpacked `.hnmod` / a bundle folder (one holding `horizon-mixin.toml`).

The site's **Mods** panel uploads to this folder for you. Every build includes all of them unless you pick
some; `mods = none` builds a clean client.

Supported: mixin mods (injections, hook points, helper Rust, `[[crate]]`, `data/`). Not supported yet:
`[api]` trait mods and `[[post_build]]` steps (the build says so by name instead of building something wrong).
