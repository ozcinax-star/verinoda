# Verinoda Symbiosis

Verinoda and Claude Code, working as one: this repository is [Verinoda](https://github.com/ozcinax-star/verinoda)
(evidence-first code analysis for coding agents) together with **verinoda-live**, a Claude Code mod that lives in
the session beside the agent. Verinoda's own code here is the upstream branch it was built on plus the changes
proposed back to it; the mod is `claude-mods/verinoda-live/`.

## Why a mod

Measured with a model in the loop (57 questions, 399 Claude Code sessions, pre-registered;
`benchmarks/results/agent-compare-2026-10-02/`):

- **Offered** Verinoda, the agent used it in 6 of 57 sessions. The tool was not the problem; adoption was.
- **Told to start with Verinoda's analyze**, it found the same facts as searching by hand at 16 % fewer input tokens,
  22 % fewer output tokens and 29 % fewer tool calls; the 95 % intervals exclude the noise floor.
- **Handed search results up front**, it spent less but found a few facts fewer: leads anchor it.

The mod does the measured thing, in the session, without being asked each time.

## What verinoda-live does

| | |
|---|---|
| **Keeps the index fresh** | Every Edit / Write / NotebookEdit in the project is noted; at the end of the turn one `verinoda update --fast` runs (about 20 s on 2,900 files instead of 130-155 s; `benchmarks/results/mod-live-self-quiet-2026-10-02/`), and the background graph build is watched until it has caught up. Edits during a build wait for it; a failed update keeps its files. |
| **Nudges the agent** | `/verinoda-auto nudge`: a code question typed in the project carries the instruction to start with `verinoda analyze` (the measured winner); `search` attaches `verinoda query` results instead; `off` by default. |
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

Then `/verinoda-panel`, and `/verinoda-auto nudge` if you want the measured behaviour.

## Develop it

`claude plugin validate claude-mods/verinoda-live` and `claude plugin test claude-mods/verinoda-live` (35 tests on the
terminal and desktop surfaces). Its design, the three-lens review it went through and the user-eyes redesign of the
pane are in `claude-mods/verinoda-live/README.md`.

---

## Türkçe özet

Verinoda Symbiosis, Verinoda ile Claude Code'u birlikte çalıştırır. **verinoda-live** modu oturumun içinde yaşar:

- Claude dosya düzenledikçe indeksi tazeler.
- Ajanı ölçülmüş en iyi davranışa yönlendirir: önce `analyze`. Bu, aynı doğrulukla %16 daha az girdi token'ı ve %29 daha az araç çağrısı demek.
- Commit'leri inceler.
- Bunların hepsini hareketli mor maskotlu, Türkçe bir panelde gösterir.

Kurulum için yukarıdaki üç satır yeterli. Kurulumdan sonra `/verinoda-panel` ve `/verinoda-auto nudge` komutlarını kullanın.
