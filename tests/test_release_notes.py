import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import release_notes as rn  # noqa: E402

REQ, BUILT = "c" * 40, "a" * 40
BASE = {"SHA": BUILT, "REQUESTED_SHA": REQ, "LABEL": "Nightly 2026-10-30 · aaaaaaa", "ASKED": "Nightly 2026-11-13 · ccccccc",
        "PROFILE": "fast", "MIXINS": "d" * 40, "GITHUB_RUN_ID": "7", "RUN_URL": "https://example/runs/7"}


def info_of(text):
    return json.loads(re.search(r"<!-- build-info (.*) -->", text).group(1))


class Notes(unittest.TestCase):
    def test_a_fallback_names_the_commit_and_the_mods(self):
        fallback = {"requested_hooks_api": 415, "unsupported": [{"id": "coords", "version": "1", "why": "pinned to aaaaaaa"}],
                    "attempts": [{"horizon": "b" * 40, "ok": False, "error": "error: x --> y"}, {"horizon": BUILT, "ok": True}]}
        mods = [{"id": "coords", "version": "1", "hooks": 0, "horizon": BUILT, "supports_requested": False}]
        text = rn.notes({**BASE, "MODS_JSON": json.dumps(mods), "FALLBACK_JSON": json.dumps(fallback)})
        self.assertIn("**Fallback commit:**", text)
        self.assertIn(f"/commit/{BUILT})", text)
        self.assertIn("`coords 1`: pinned to aaaaaaa", text)
        info = info_of(text)
        self.assertTrue(info["fell_back"])
        self.assertEqual((info["requested"]["short"], info["built"]["short"]), ("ccccccc", "aaaaaaa"))
        self.assertEqual([a["ok"] for a in info["attempts"]], [False, True])
        # the first commit link is the built one: older versions of the site read it that way
        self.assertEqual(re.search(r"/commit/([0-9a-f]{40})", text).group(1), BUILT)
        # an error containing "-->" can't close the hidden comment early: its JSON holds no "-->" and is the last thing
        payload = re.search(r"<!-- build-info (.*) -->", text).group(1)
        self.assertNotIn("-->", payload)
        self.assertTrue(text.rstrip().endswith("-->"))
        self.assertEqual(info["attempts"][0]["error"], "error: x --> y")

    def test_no_mods_means_a_plain_note(self):
        text = rn.notes({**BASE, "SHA": REQ, "LABEL": BASE["ASKED"], "MODS_JSON": "[]", "FALLBACK_JSON": ""})
        self.assertIn("- Mods: none", text)
        self.assertNotIn("Fallback", text)
        self.assertFalse(info_of(text)["fell_back"])

    def test_support_on_the_requested_commit_is_said_so(self):
        mods = [{"id": "hud", "version": "2", "hooks": 400, "horizon": BUILT, "supports_requested": True}]
        fallback = {"requested_hooks_api": 645, "unsupported": [], "attempts": [{"horizon": REQ, "ok": True}]}
        text = rn.notes({**BASE, "SHA": REQ, "LABEL": BASE["ASKED"], "MODS_JSON": json.dumps(mods),
                         "FALLBACK_JSON": json.dumps(fallback)})
        self.assertIn("Built on the commit you asked for", text)
        self.assertNotIn("Commits tried", text)


if __name__ == "__main__":
    unittest.main()
