"""Offline tests of scripts/prepare_mods.py: no network, no cargo, no Horizon source needed.

    python3 -m unittest discover -s tests -v
"""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import prepare_mods as pm  # noqa: E402

OLD, MID, NEW = "a" * 40, "b" * 40, "c" * 40  # three Horizon commits, oldest first
MIXINS = "d" * 40


def make_hnmod(folder, mod_id, horizon, hooks=None):
    """A minimal valid .hnmod: the manifest and a bundle whose mixin file agrees with it."""
    meta = {"schema": 1, "id": mod_id, "version": "1.0.0", "game": "26.3", "horizon": horizon, "mixins": MIXINS}
    if hooks is not None:
        meta["hooks"] = hooks
    toml = f'[mod]\nid = "{mod_id}"\nversion = "1.0.0"\n\n[compatibility]\nhorizon_revision = "{horizon}"\n'
    with zipfile.ZipFile(pathlib.Path(folder) / f"{mod_id}.hnmod", "w") as zf:
        zf.writestr("horizon-native.json", json.dumps(meta))
        zf.writestr(f"mixins/{mod_id}/horizon-mixin.toml", toml)


class Extract(unittest.TestCase):
    def run_extract(self, count):
        with tempfile.TemporaryDirectory() as tmp:
            up = pathlib.Path(tmp, "up")
            up.mkdir()
            for i in range(count):
                make_hnmod(up, f"mod_{i:03d}", OLD)
            pm.extract(types.SimpleNamespace(uploads=str(up), out=str(pathlib.Path(tmp, "out"))))
            return json.loads(pathlib.Path(tmp, "out", "mods.json").read_text())

    def test_more_than_ten_mods_are_accepted(self):
        self.assertEqual(len(self.run_extract(15)), 15)

    def test_the_cap_is_a_runaway_guard_not_ten(self):
        self.assertGreaterEqual(pm.MAX_MODS, 50)
        with self.assertRaises(SystemExit):
            self.run_extract(pm.MAX_MODS + 1)


class Support(unittest.TestCase):
    def test_hook_mods_fit_commits_with_a_new_enough_api(self):
        mod = {"hooks": 427, "horizon": OLD}
        self.assertEqual(pm.supports(mod, NEW, 645), (True, ""))
        ok, why = pm.supports(mod, NEW, 415)
        self.assertFalse(ok)
        self.assertEqual(why, f"needs hook API 427; {NEW[:7]} has 415")
        self.assertFalse(pm.supports(mod, NEW, None)[0])

    def test_pinned_mods_fit_only_their_commit(self):
        mod = {"hooks": 0, "horizon": OLD}
        self.assertTrue(pm.supports(mod, OLD, 1)[0])
        self.assertEqual(pm.supports(mod, NEW, 645), (False, f"pinned to {OLD[:7]}"))

    def test_short_error_picks_the_error_line(self):
        text = "Indexing...\nerror: mod coords_hud: no target Game::native_foo\n  at x\n"
        self.assertEqual(pm.short_error(text), "error: mod coords_hud: no target Game::native_foo")
        self.assertEqual(pm.short_error(""), "failed")
        self.assertLessEqual(len(pm.short_error("error " + "x" * 1000)), 240)


class Stage(unittest.TestCase):
    """stage() with the network, cargo and horizon-mixins replaced: the newest pin fails, an older one works."""

    def test_fallback_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            up = tmp / "up"
            up.mkdir()
            make_hnmod(up, "newer_mod", MID)      # pinned to a commit older than the one asked for
            make_hnmod(up, "older_mod", OLD)      # pinned to an even older one
            pm.extract(types.SimpleNamespace(uploads=str(up), out=str(tmp / "prepared")))
            tree = tmp / "horizon"
            hooks = tree / "crates/horizon-client/src/game/native_hooks.rs"
            hooks.parent.mkdir(parents=True)
            hooks.write_text("pub(crate) const NATIVE_HOOKS_API: u32 = 415;\n")
            (tree / ".horizon-revision").write_text(NEW)

            applied = []

            def fake_run(cmd, **kwargs):
                if "apply" in cmd:
                    workspace = pathlib.Path(cmd[cmd.index("--workspace") + 1])
                    revision = (workspace / ".horizon-revision").read_text().strip()
                    applied.append(revision)
                    if revision == MID:
                        return types.SimpleNamespace(returncode=1, stdout="", stderr="error: older_mod: target missing\n")
                    pathlib.Path(cmd[cmd.index("--out") + 1]).mkdir(parents=True)
                    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
                # fetch_source.py REVISION TREE
                (pathlib.Path(cmd[-1]) / ".horizon-revision").write_text(cmd[-2])
                return types.SimpleNamespace(returncode=0)

            dates = {OLD: "2026-10-01T00:00:00Z", MID: "2026-10-20T00:00:00Z", NEW: "2026-11-13T00:00:00Z",
                     MIXINS: "2026-10-01T00:00:00Z"}
            saved = (pm.date_key, pm.mixins_revision, pm.build_cli, pm.subprocess.run)
            pm.date_key = lambda revision, repository=pm.UPSTREAM: dates[revision]
            pm.mixins_revision = lambda tree: MIXINS
            pm.build_cli = lambda revision, root: (pathlib.Path("horizon-mixins"), False)
            pm.subprocess.run = fake_run
            try:
                pm.stage(argparse.Namespace(mods=str(tmp / "prepared"), requested=NEW, horizon_root=str(tree),
                                            fetch_source="fetch_source.py", mixins_cache=str(tmp / "cache"),
                                            out=str(tmp / "out")))
            finally:
                pm.date_key, pm.mixins_revision, pm.build_cli, pm.subprocess.run = saved

            chosen = json.loads((tmp / "out" / "chosen.json").read_text())
            fb = chosen["fallback"]
            self.assertEqual(applied, [MID, OLD])       # newest pin first, then the older one
            self.assertEqual(chosen["horizon"], OLD)
            self.assertTrue(fb["fell_back"])
            self.assertEqual((fb["requested"], fb["built"], fb["requested_hooks_api"]), (NEW, OLD, 415))
            self.assertEqual([a["ok"] for a in fb["attempts"]], [False, True])
            self.assertEqual(fb["attempts"][0]["error"], "error: older_mod: target missing")
            self.assertEqual({u["id"] for u in fb["unsupported"]}, {"newer_mod", "older_mod"})
            self.assertTrue(all(not m["supports_requested"] for m in chosen["mods"]))


if __name__ == "__main__":
    unittest.main()
