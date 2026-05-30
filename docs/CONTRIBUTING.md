# Contributing

Notes for working on this fork.

---

## Repo layout

```
Telegram_Leecher.ipynb     ← the Colab notebook (launcher cell == main.py)
main.py                    ← the Colab form cell, kept in sync with the notebook
requirements.txt
colab_leecher/
  __init__.py              ← credentials + asyncio loop + Pyrogram Client
  __main__.py              ← command handlers + callbacks
  downlader/               ← per-source downloaders (aria2, gdrive, mega, telegram, terabox, ytdl, s3)
  uploader/                ← telegram.py, s3.py
  utility/                 ← task_manager, handler, converters, helper, variables, s3_iter
docs/                      ← all documentation
```

See [ARCHITECTURE.md](./ARCHITECTURE.md) for how the pieces connect.

---

## Keeping `main.py` and the notebook in sync

The notebook's main code cell **must** be byte-identical to `main.py`. When you change `main.py`, regenerate the notebook cell. A quick check:

```python
import json, pathlib
nb = json.loads(pathlib.Path("Telegram_Leecher.ipynb").read_text())
cell = "".join(nb["cells"][3]["source"])         # index of the main code cell
assert cell.strip() == pathlib.Path("main.py").read_text().strip(), "OUT OF SYNC"
print("in sync")
```

---

## Local sanity checks (no PyPI / Telegram needed)

The sandbox can't install `boto3`/`pyrogram`, but you can still validate syntax and pure logic:

```bash
# 1. Every module must parse
python3 - <<'PY'
import ast, glob
for f in glob.glob("colab_leecher/**/*.py", recursive=True):
    ast.parse(open(f).read()); print("OK", f)
PY

# 2. main.py parses if you strip the ! shell-magic lines (Colab-only)
python3 -c "import ast; ast.parse('\n'.join(l for l in open('main.py') if not l.lstrip().startswith('!')))"
```

For logic-only functions (URI parsing, tracker dedup, split-size math) write a tiny stub-import script that mocks the heavy deps and exercises the real functions — see prior commits for examples.

---

## Conventions

- **Don't break the bulk path.** S3 features are additive; `/tupload`, `/gdupload`, etc. must keep working unchanged.
- **One split decision.** All Telegram-bound uploads go through `sizeChecker`. Don't add a second size check elsewhere — extend `sizeChecker` instead.
- **Tracker is best-effort.** Tracker read/write/remote-mirror failures must log and continue, never abort a transfer.
- **Fail loudly on data loss.** If an upload can't happen (e.g. oversized part), record it in `Transfer.failed_files` and surface it in `SendLogs`. Never report `COMPLETE` while data is missing.
- **Resume safety.** In iterate mode, only mark an object done after a fully successful round-trip with zero failed parts.
- **Lazy imports for S3.** `boto3` is optional; import it inside functions so the bot still runs for non-S3 users.

---

## Commit / PR style

- Small, focused commits with a clear subject line and a body explaining the *why*.
- Branch naming: `feat/...`, `fix/...`, `docs/...`.
- Push the branch and open a PR against `main`. Describe what changed, how it was verified, and any limitations.
- Never force-push `main`.

---

## Testing a change in Colab

1. Push your branch.
2. In the notebook form, set `REPO_BRANCH` to your branch name.
3. Fully **disconnect and delete** the Colab runtime, then re-open the notebook fresh (Colab caches open notebooks).
4. Run — the launcher clones your branch and applies the post-clone safety patch.

---

## Releasing

Merge the PR into `main`. Users open the notebook from the `main` badge, which clones `main`. Because the launcher does a fresh `git clone` every run, merged changes reach users on their next run with no extra steps.

---

## Wiki integration

The GitHub Wiki is **generated from `docs/`** — it is not edited by hand. This keeps a single source of truth.

**How it works**

1. You edit `docs/*.md` (and `README.md`) as normal.
2. `scripts/build_wiki.py` converts each doc into a wiki page, rewriting inter-doc links (e.g. `./docs/S3_GUIDE.md#anchor` → `S3-Guide#anchor`) and generating `Home.md`, `_Sidebar.md`, and `_Footer.md`.
3. The `.github/workflows/publish-wiki.yml` workflow runs that script on every push to `main` that touches `docs/`, `README.md`, or the script, then pushes the result to the `<repo>.wiki.git` repository.

**Page name mapping** (see `DOC_TO_WIKI` in the script):

| Repo doc | Wiki page |
|---|---|
| `docs/SETUP.md` | Setup |
| `docs/COMMANDS.md` | Commands |
| `docs/S3_GUIDE.md` | S3-Guide |
| `docs/SPLIT_AND_UPLOAD.md` | Split-and-Upload |
| `docs/ARCHITECTURE.md` | Architecture |
| `docs/FAQ.md` | FAQ |
| `docs/TROUBLESHOOTING.md` | Troubleshooting |
| `docs/CONTRIBUTING.md` | Contributing |

**One-time setup:** the wiki repo only exists after the wiki is initialised once. Go to the repo's **Wiki** tab → create the first page → **Save**. After that, the workflow can clone and push to it. (Until then, the workflow logs a clear warning and exits.)

**Preview locally** before pushing:

```bash
python3 scripts/build_wiki.py --out build/wiki
ls build/wiki/        # Home.md, Setup.md, S3-Guide.md, _Sidebar.md, …
```

`build/` is git-ignored — never commit generated pages. Add a new doc by dropping it in `docs/` and adding an entry to `DOC_TO_WIKI` (and `WIKI_ORDER` for nav) in `scripts/build_wiki.py`.
