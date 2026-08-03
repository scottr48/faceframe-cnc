# Resume — Faceframe Nesting Optimizer + NC Generator

Handoff doc. Read this first, then
`docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md` (the full build spec —
including the **Amendments** section at the end, which overrides the body).

## What this project is

Python desktop app for Eagle Woodworking: parses a cabinet order spreadsheet,
computes faceframe opening geometry, nests frames onto 49x97 MDF sheets
(including placing small frames INSIDE larger frames' openings — the
capability their commercial CAM lacks), shows an editable GUI layout, and
generates machine-ready .anc NC code matching their existing Fanuc-style post
exactly. Must run fully offline on a Windows shop PC. Rule zero: where the
spec and the reference `.anc` files disagree, the .anc files win.

## Environment

- Working dir: `c:\Projects\faceframe-cnc`; GitHub `scottr48/faceframe-cnc`
- Python 3.14.2 system-wide; `.venv` has pandas 3.0.5 + xlrd 2.0.2 (verified
  working). Geometry/nesting/anc code is stdlib-only; only the .xls parser
  needs the venv.
- Tests: `python -m unittest discover tests` (parser tests skip without
  pandas); run via `.venv\Scripts\python.exe` to include parser tests.

## Working style (per Scott — do not deviate)

- Fable does NOT write code: delegate to Sonnet (routine) / Opus (complex)
  subagents, review their output, accept or send back fixes. Light fixes
  Fable may do directly.
- Check in with Scott after each milestone; only commit when asked.

## State: Milestones 1 and 2 complete and reviewed

- `faceframe_cnc/geometry.py` — frame-type inference + opening geometry.
  WDC is special (2026-08-03 amendment): 2" stiles, WDC2436 is 18x36 →
  opening 14x33. Openings validated against real machine file.
- `faceframe_cnc/order_parser.py` — .xls parser (pandas+xlrd), needs-attention
  flow (7-21 order: WDC2436 missing width → resolve with 18; SD1212 excluded).
- `faceframe_cnc/anc_reader.py` — decodes T11 cut rectangles from .anc files;
  cross-check proves engine matches R730101N.anc exactly (tool center inset
  0.1975 = 0.1875 radius + 0.010 finish stock for T12).
- `faceframe_cnc/nesting.py` — Milestone 2 footprint optimizer: pattern-based
  (stamp out identical sheets as runs), exact-knapsack shelves, 0.375" gap,
  soft 0.5" edge cushion, deterministic; independent `validate_layouts`.
  7-21 order: 47 sheets, 16 unique pictures, 85.8% fill (area floor 41).
  `Placement.children` reserved for M3 inners.

## Next: Milestone 3 — frame-inside-frame (spec §4b)

Host eligibility incl. WDC as wall-style host; clearance rule
inner + 0.75 ≤ opening per axis with rotation; center inners; prefer
smallest inner, one per host; verify amended candidate table (WDC2436 row:
only rotated W3012 fits its 14x33 opening). Baseline to beat: 47 sheets.
Then M4 GUI, M5 NC post, M6 PDF report.
