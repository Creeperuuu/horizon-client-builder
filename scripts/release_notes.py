#!/usr/bin/env python3
"""Write a build's release notes (markdown) to stdout, from environment variables set by build.yml.

Besides the text for people, the notes end with a hidden `<!-- build-info {...} -->` block that the site reads:
which commit was asked for, which was really built, which mods don't support the one asked for, and the
commits that were tried before the one that worked.
"""
import json
import os

UPSTREAM = os.environ.get("UPSTREAM", "horizon-rust/horizon")


def commit_link(sha):
    return f"[`{sha[:7]}`](https://github.com/{UPSTREAM}/commit/{sha})"


def build_info(env):
    """The facts about this build as a dict (what the site reads)."""
    fallback = json.loads(env.get("FALLBACK_JSON") or "null") or {}
    built = env["SHA"]
    requested = env.get("REQUESTED_SHA") or built
    mods = json.loads(env.get("MODS_JSON") or "[]")
    return {
        "requested": {"sha": requested, "short": requested[:7], "label": env.get("ASKED", "")},
        "built": {"sha": built, "short": built[:7], "label": env.get("LABEL", "")},
        "fell_back": built != requested,
        "requested_hooks_api": fallback.get("requested_hooks_api"),
        "unsupported": fallback.get("unsupported", []),
        "attempts": [{"sha": a["horizon"], "short": a["horizon"][:7], "ok": a["ok"], "error": a.get("error", "")}
                     for a in fallback.get("attempts", [])],
        "mods": [{"id": m["id"], "version": m["version"], "hooks": m.get("hooks", 0), "horizon": m["horizon"],
                  "supports_requested": m.get("supports_requested", built == requested)} for m in mods],
    }


def notes(env):
    info = build_info(env)
    built, requested = info["built"], info["requested"]
    mods = ", ".join(f"`{m['id']} {m['version']}`" for m in info["mods"]) or "none"
    lines = [
        f"**horizon-client.exe** for Windows x64, built from {commit_link(built['sha'])}.",
        "",
        f"- Revision: {built['label']}",
        f"- Profile: `{env['PROFILE']}`",
        f"- Mods: {mods}",
    ]
    if info["mods"]:
        lines.append(f"- horizon-mixins `{env.get('MIXINS', '')[:7]}`")
        if info["fell_back"]:
            lines.append(f"- **Fallback commit:** you asked for {requested['label']} ({commit_link(requested['sha'])}), "
                         f"but some mods don't support it, so it was built on {commit_link(built['sha'])} "
                         f"({built['label']}) instead.")
            for u in info["unsupported"]:
                lines.append(f"  - `{u['id']} {u['version']}`: {u['why']}")
            if not info["unsupported"]:
                lines.append("  - the mods pin other commits")
        else:
            lines.append(f"- Built on the commit you asked for ({requested['label']}): every mod applies to it.")
        tried = info["attempts"]
        if len(tried) > 1 or any(not a["ok"] for a in tried):
            lines.append("- Commits tried, newest mod pin first:")
            for a in tried:
                lines.append(f"  - {commit_link(a['sha'])}: " + ("built" if a["ok"] else f"did not apply ({a['error']})"))
    lines.append(f"- Built by [run {env['GITHUB_RUN_ID']}]({env['RUN_URL']})")
    lines += ["", "Unsigned. Check it against `SHA256SUMS`. To play it, import it in the Horizon launcher "
                  "(Versions, import a build): the program alone needs a version folder's game files.", ""]
    # '<' and '>' are escaped so the JSON can never end the comment early.
    payload = json.dumps(info, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
    lines.append(f"<!-- build-info {payload} -->")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(notes(os.environ), end="")
