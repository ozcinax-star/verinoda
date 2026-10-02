# Verinoda Symbiosis

Verinoda and Claude Code, working as one: this repository is [Verinoda](https://github.com/ozcinax-star/verinoda)
(evidence-first code analysis for coding agents) together with **verinoda-live**, a Claude Code mod that lives in
the session beside the agent. Verinoda's own code here is the upstream branch it was built on plus the changes
proposed back to it; the mod is `claude-mods/verinoda-live/`.

## Why a mod

Measured with a model in the loop, pre-registered:

- **Offered** Verinoda, the agent used it in 6 of 57 sessions (`benchmarks/results/agent-compare-2026-10-02/`). An
  index nobody calls and nobody refreshes is no help; the mod keeps it fresh and can put it in the agent's path.
- **Told to start with Verinoda's analyze**, it found the same facts as searching by hand in both studies. On
  corpora of 37-226 files that came at 16 % fewer input tokens and 29 % fewer tool calls; on the ten real-world
  repositories (`benchmarks/results/agent-compare-realworld-2026-10-02/`) at 14 % *more* input tokens and 16 %
  fewer output tokens. Neither study found more facts with it.
- **Handed search results up front**, it found a few facts fewer: leads anchor it.
- **Checking each edit** (`benchmarks/results/agent-compare-guard-2026-10-02/`, 60 real coding sessions with hidden
  tests): with the check and without it the agent passed every test and wrote no name that does not exist; it read
  the code before every edit, so the check had nothing to catch. It stays on as a cheap safety net.
- **After the code moved on** (`benchmarks/results/agent-compare-stale-2026-10-02/`): a fresh index and a stale
  one found the same facts, and no stale index made the agent describe removed code. Freshness paid off in cost,
  and only on projects too large for `analyze` to refresh itself (300+ files): there a stale index cost about a
  fifth more input tokens than none, and the fresh index the mod keeps removed that (in one of two runs).

## What verinoda-live does

| | |
|---|---|
| **Keeps the index fresh** | Every Edit / Write / NotebookEdit in the project is noted; at the end of the turn one `verinoda update --fast` runs (about 20 s on 2,900 files instead of 130-155 s; `benchmarks/results/mod-live-self-quiet-2026-10-02/`), and the background graph build is watched until it has caught up. Edits during a build wait for it; a failed update keeps its files. |
| **Nudges the agent** | `/verinoda-auto nudge`: a code question typed in the project carries the instruction to start with `verinoda analyze` (same facts; fewer output, more input tokens on real repositories); `search` attaches `verinoda query` results instead; `off` by default. |
| **Reviews commits** | `/verinoda-guard on`: when a Bash or PowerShell command moved HEAD, `verinoda review` runs in the background and a toast gives the risk and findings. |
| **Shows it** | `/verinoda-panel`: a Turkish pane with the index state (glyph + words + colour), the last commit review, both settings with hotkeys, and an animated purple Claude mascot in glasses that reads the code while Verinoda works. |

Settings (`/config`): `root` (project folder), `cli` (Verinoda program), `python` (a Python that imports verinoda),
`motion` (the mascot's animation). Empty means found: the nearest folder with a `.verinoda` index (never a home
folder or a drive root), the project's `.venv` CLI or `verinoda` on PATH, the Python beside it.

## Use it

```
pip install verinoda            # or: uv tool install verinoda
cd your-project && verinoda setup .
claude --plugin-dir <this repository>/claude-mods/verinoda-live
```

Then `/verinoda-panel`; `/verinoda-auto nudge` if you want the agent to start from Verinoda (see above for what it costs).

## Develop it

`claude plugin validate claude-mods/verinoda-live` and `claude plugin test claude-mods/verinoda-live` (35 tests on the
terminal and desktop surfaces). Its design, the three-lens review it went through and the user-eyes redesign of the
pane are in `claude-mods/verinoda-live/README.md`.

---

## Türkçe özet

Verinoda Symbiosis, Verinoda ile Claude Code'u birlikte çalıştırır. **verinoda-live** modu oturumun içinde yaşar:

- Claude dosya düzenledikçe indeksi tazeler.
- İstenirse ajanı önce `analyze` çalıştırmaya yönlendirir. İki ölçümde de aynı olguları buldu; küçük projelerde %16 daha az, gerçek depolarda ise %14 daha fazla girdi token'ı harcadı (çıktı %16 daha az).
- Kod değiştikten sonra sorulan 50 soruda taze ve bayat indeks aynı olguları buldu; bayat indeks ajanı silinmiş kodu anlatmaya yöneltmedi. Tazeliğin faydası maliyette ve yalnızca büyük projelerde (300+ dosya) görüldü: bayat indeks indekssiz çalışmaya göre ~%20 daha fazla girdi token'ı harcattı, taze indeks bunu bir çalışmada geri aldı.
- Claude'un her Python/Java/Kotlin düzenlemesini kontrol edip var olmayan isimleri söyler. 60 gerçek kodlama oturumunda modlu ve modsuz ajan bütün gizli testleri geçti; ajan her düzenlemeden önce kodu okuduğu için kontrolün yakalayacağı bir şey çıkmadı.
- Commit'leri inceler.
- Bunların hepsini hareketli mor maskotlu, Türkçe bir panelde gösterir.

Kurulum için yukarıdaki üç satır yeterli. Kurulumdan sonra `/verinoda-panel`; ajanın Verinoda'dan başlamasını isterseniz `/verinoda-auto nudge`.
