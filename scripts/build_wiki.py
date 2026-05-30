#!/usr/bin/env python3
# copyright 2024 © Xron Trix | https://github.com/Xrontrix10
"""Generate GitHub Wiki pages from the in-repo ``docs/`` folder.

This is the wiki integration's single source of truth: documentation is
authored once in ``docs/*.md`` (and ``README.md``) and this script
converts it into wiki pages under a build directory, ready to be pushed
to the project's GitHub Wiki by ``.github/workflows/publish-wiki.yml``.

What it does
------------
1. Maps each repo doc file to a wiki page name, e.g.
   ``docs/S3_GUIDE.md`` -> ``S3-Guide.md`` (wiki page "S3-Guide").
2. Rewrites inter-doc Markdown links to wiki links, e.g.
   ``[x](./docs/SETUP.md)`` -> ``[x](Setup)`` and
   ``[x](./SPLIT_AND_UPLOAD.md#anchor)`` -> ``[x](Split-and-Upload#anchor)``.
3. Generates ``Home.md`` (landing page), ``_Sidebar.md`` (nav) and
   ``_Footer.md`` (footer) automatically.

Run locally:  python3 scripts/build_wiki.py --out build/wiki
The workflow runs the same command and pushes ``build/wiki`` to the wiki.
"""

import argparse
import os
import re
import pathlib

# repo-doc filename  ->  wiki page name (without .md)
DOC_TO_WIKI = {
    "docs/SETUP.md": "Setup",
    "docs/COMMANDS.md": "Commands",
    "docs/S3_GUIDE.md": "S3-Guide",
    "docs/SPLIT_AND_UPLOAD.md": "Split-and-Upload",
    "docs/ARCHITECTURE.md": "Architecture",
    "docs/FAQ.md": "FAQ",
    "docs/TROUBLESHOOTING.md": "Troubleshooting",
    "docs/CONTRIBUTING.md": "Contributing",
}

# Human-friendly titles for the sidebar / home, in display order.
WIKI_ORDER = [
    ("Setup", "🚀 Setup guide"),
    ("Commands", "🤖 Bot commands"),
    ("S3-Guide", "☁️ S3 / Wasabi deep dive"),
    ("Split-and-Upload", "✂️ >2 GB splitting & upload"),
    ("Architecture", "🧭 Architecture"),
    ("FAQ", "❓ FAQ"),
    ("Troubleshooting", "🩹 Troubleshooting"),
    ("Contributing", "🛠️ Contributing"),
]

# Build a lookup of every basename (and ./docs/ form) -> wiki page name so
# links can be rewritten regardless of how they were written in the doc.
_LINK_TARGETS = {}
for _doc, _wiki in DOC_TO_WIKI.items():
    base = os.path.basename(_doc)            # SETUP.md
    _LINK_TARGETS[f"./{_doc}"] = _wiki       # ./docs/SETUP.md
    _LINK_TARGETS[_doc] = _wiki              # docs/SETUP.md
    _LINK_TARGETS[f"./docs/{base}"] = _wiki  # ./docs/SETUP.md (explicit)
    _LINK_TARGETS[f"docs/{base}"] = _wiki
    _LINK_TARGETS[f"./{base}"] = _wiki       # ./SETUP.md (intra-docs links)
    _LINK_TARGETS[base] = _wiki              # SETUP.md

# README links to ./docs/X.md; README itself becomes Home (handled separately).
_LINK_TARGETS["./README.md"] = "Home"
_LINK_TARGETS["README.md"] = "Home"

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def rewrite_links(text: str) -> str:
    """Rewrite Markdown links that point at other docs to wiki page links."""

    def repl(m):
        label, target = m.group(1), m.group(2)
        # Split off an optional #anchor.
        anchor = ""
        if "#" in target:
            target, anchor = target.split("#", 1)
            anchor = "#" + anchor
        # Leave external links (http, mailto, etc.) and pure anchors untouched.
        if target.startswith(("http://", "https://", "mailto:")) or target == "":
            return m.group(0)
        key = target.strip()
        wiki = _LINK_TARGETS.get(key)
        if wiki is None:
            # Try a normalized basename match as a last resort.
            wiki = _LINK_TARGETS.get(os.path.basename(key))
        if wiki is None:
            return m.group(0)  # unknown internal link — leave as-is
        return f"[{label}]({wiki}{anchor})"

    return _LINK_RE.sub(repl, text)


def build(out_dir: str, repo_root: str):
    root = pathlib.Path(repo_root)
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. Convert each doc into its wiki page.
    for doc, wiki in DOC_TO_WIKI.items():
        src = root / doc
        if not src.exists():
            print(f"  WARN: {doc} missing, skipping")
            continue
        content = rewrite_links(src.read_text())
        (out / f"{wiki}.md").write_text(content)
        print(f"  {doc} -> {wiki}.md")

    # 2. Sidebar (appears on every wiki page).
    sidebar = ["### 📖 Colab Leecher Wiki", ""]
    sidebar.append("- [Home](Home)")
    for wiki, title in WIKI_ORDER:
        sidebar.append(f"- [{title}]({wiki})")
    sidebar += [
        "",
        "---",
        "[⭐ Repository](https://github.com/ajithvnr2001/Telegram-Leecher)",
    ]
    (out / "_Sidebar.md").write_text("\n".join(sidebar) + "\n")
    print("  generated _Sidebar.md")

    # 3. Footer.
    footer = (
        "_Auto-generated from [`docs/`](https://github.com/ajithvnr2001/"
        "Telegram-Leecher/tree/main/docs) by `scripts/build_wiki.py`. "
        "Edit the docs, not the wiki._\n"
    )
    (out / "_Footer.md").write_text(footer)
    print("  generated _Footer.md")

    # 4. Home landing page.
    home = [
        "# Colab Leecher Wiki",
        "",
        "A Pyrogram-based Telegram bot that transfers files to **Telegram**, "
        "**Google Drive** and **S3-compatible storage** (AWS, Wasabi, Backblaze "
        "B2, Cloudflare R2, DigitalOcean Spaces, MinIO, and more) using Google "
        "Colab — with whole-bucket iterative processing, crash-resume, and a "
        "bidirectional `s3teletracker.json` audit log.",
        "",
        "> 📓 Open the bot in Colab: "
        "[Telegram_Leecher.ipynb](https://colab.research.google.com/github/"
        "ajithvnr2001/Telegram-Leecher/blob/main/Telegram_Leecher.ipynb)",
        "",
        "## Pages",
        "",
    ]
    for wiki, title in WIKI_ORDER:
        home.append(f"- **[{title}]({wiki})**")
    home += [
        "",
        "## Quick command reference",
        "",
        "| Command | What it does |",
        "|---|---|",
        "| `/tupload` | Leech links to Telegram |",
        "| `/gdupload` | Mirror to Google Drive |",
        "| `/ytupload` | Force the yt-dlp pipeline |",
        "| `/drupload` | Leech a local Colab folder |",
        "| `/s3upload` | Mirror any source to your S3 bucket |",
        "| `/s3leech` | Leech `s3://bucket/key`, a prefix, or a whole bucket to Telegram |",
        "| `/s3bucket <name>` | Change destination S3 bucket at runtime |",
        "| `/s3prefix <folder>` | Set/clear destination key prefix |",
        "",
        "See **[Commands](Commands)** for the full reference.",
        "",
    ]
    (out / "Home.md").write_text("\n".join(home) + "\n")
    print("  generated Home.md")

    pages = sorted(p.name for p in out.glob("*.md"))
    print(f"\nBuilt {len(pages)} wiki pages into {out}/")
    return pages


def main():
    ap = argparse.ArgumentParser(description="Build GitHub Wiki pages from docs/")
    ap.add_argument("--out", default="build/wiki", help="output directory")
    ap.add_argument("--repo-root", default=".", help="repository root")
    args = ap.parse_args()
    build(args.out, args.repo_root)


if __name__ == "__main__":
    main()
