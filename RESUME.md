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

## State: Milestones 1-5 complete and reviewed

- `faceframe_cnc/geometry.py` — frame-type inference + opening geometry.
  WDC is special (2026-08-03 amendment): 2" stiles, WDC2436 is 18x36 →
  opening 14x33. Openings validated against real machine file.
- `faceframe_cnc/order_parser.py` — .xls parser (pandas+xlrd), needs-attention
  flow. 2026-08-03 amendment: a WDC row missing exactly one dim is
  auto-resolved from the part number (frame W = encoded cabinet W − 6, so
  WDC2436 → 18×36) with a provenance note — no prompt; a contradicting dim
  still needs attention, and SD1212 (both dims N/A) stays auto-excluded.
  The 7-21 order now loads with an EMPTY needs-attention list.
- `faceframe_cnc/anc_reader.py` — decodes T11 cut rectangles from .anc files;
  cross-check proves engine matches R730101N.anc exactly (tool center inset
  0.1975 = 0.1875 radius + 0.010 finish stock for T12).
- `faceframe_cnc/nesting.py` — Milestone 2 footprint optimizer: pattern-based
  (stamp out identical sheets as runs), exact-knapsack shelves, **0.455"
  gap** (measured; see below), soft 0.5" edge cushion, deterministic;
  independent `validate_layouts`. 7-21 order: 49 sheets footprint-only
  (area floor 41). `Placement.children` carries M3 inners.

- `faceframe_cnc/inside.py` + nesting.py M3 extensions — frame-inside-frame
  (spec 4b): exact max-flow inner/host assignment, WDC2436 dual-role
  (hosts a rotated W3012 AND nests inside W2742/W2442), portfolio ranked
  sheets-first (nesting B18 is a net loss — it packs free beside 30" parts).
  7-21 order: 41 sheets vs 49 baseline, 80 inners, validator recurses into
  children (containment vs recomputed openings, CCW rotation convention).
- `faceframe_cnc/gui/` — Milestone 4 PySide6 GUI (PySide6 6.11.1 verified on
  Py 3.14): headless `session.py` holds ALL logic (order load/resolve/include,
  optimize, move/rotate/cross-sheet/nest/unnest edits validated on trial
  copies — invalid edits snap back with the violated rule; spec-4c run
  splitting and re-grouping). Qt widgets are thin. Launch:
  `.venv\Scripts\python.exe -m faceframe_cnc.gui` (`--self-test N` for
  headless smoke). Settings persist to faceframe_settings.json (gitignored).

- `faceframe_cnc/post/` — Milestone 5 NC post. `model.py` holds every
  measured number (tools, feeds, Z levels, offsets); `generator.py` is a
  pure template machine; `reconstruct.py` reads an .anc back into a program
  + plan; `verifier.py` is an INDEPENDENT re-parse that gates every write;
  `from_layout.py` sequences an optimizer sheet; `job.py` writes one file
  per sheet plus the dry-run (air-cut) mode. Proofs: R710101N, R720101N and
  R730101N round-trip **byte for byte**. Generated sheets use T13 → T17
  (WDC only) → T11 openings → T12 → T11 perimeters, with the onion-skin
  pass order. GUI "Generate NC" is wired up.
  - T11 opening through-cuts run tool center 0.1975 inside the opening edge
    (0.1875 radius + 0.010 T12 finish stock) — verified vs R730101N.anc.
  - **T17 WDC stile slot** (2026-08-03 amendment, grammar from RFK0101N):
    two 45° V slots per WDC frame, centreline 34 mm from the stile's INSIDE
    edge, two passes on one centreline (Z0.4062 then Z0.3125), each
    overrunning the part end by the bit's radius at that depth.
  - **WDC end clearance**: the deep pass's cone removes material 0.875 past
    each stile end, so WDC frames reserve that much against neighbours and
    the sheet edge — enforced by the packer, `validate_layouts`, the
    planner and the verifier's swept-cone check, independently.
  - Spacing (owner-approved 2026-08-03): part gap **0.455** (0.375 cannot be
    verified — the perimeter lead-in sweeps 0.425 past the part edge);
    inner-frame clearance stays **0.375** as its own setting.  0.455 is now
    a HARD FLOOR (`nesting.MIN_PART_GAP`): stale settings files are migrated
    up on load with a visible note, the settings dialog will not go lower,
    and `Session.optimize` refuses a smaller gap instead of packing sheets
    the verifier must refuse at Generate.
  - Acceptance: the 7-21-26 order at the defaults nests to 41 sheets / 17
    unique pictures and **all 17 generate and verify clean, zero refusals**,
    in production and in dry-run form.

## Where the last session left off (2026-08-03)

- Milestone 5 committed in full ("Milestone 5 complete: NC generation for
  optimized sheets, T17 WDC slot, 0.455 part gap"). Verified at commit
  time: `.venv\Scripts\python.exe -m unittest discover tests` →
  **399 tests, OK**. A hand-check of an emitted T17 section (R720102N,
  3xWDC2436) matched the amendment's geometry exactly.
- Milestone 6 (PDF report) complete, reviewed and committed:
  `faceframe_cnc/report/` (stdlib-only PDF 1.4 writer + cut-sheet composer),
  Generate flow writes `R<prefix>_report.pdf` beside the .anc files
  (default on; report failure never blocks NC). 454 tests OK (+55).
- **One session at a time in this tree.** An earlier session collided with a
  leftover coder agent from a disconnected session editing the same files
  concurrently. It merged cleanly that time; do not count on that again.

### Decisions made (Scott, 2026-08-03)

- **WDC end clearance stays** even though it costs 1 sheet on the 7-21
  order (41 vs 40): "the extra sheet is worth it for the padding."
  Do not re-raise trading it back for shallow T17 nicks in neighbours.

### Known follow-ups (recorded, non-blocking)

- The verifier checks no `F` (feed) words for any tool — a wrong feed would
  pass today. Worth its own small change + tests.
- `verifier._owner_of` attributes a move to the smallest containing grown
  box, which can self-skip a lead-in intrusion; the v-slot check has its
  own correct ownership rule. Fix someday, with a failing test first.

## Next: Milestone 6 — PDF report

One labeled page per unique sheet: the layout drawn to scale with part
numbers, the run quantity, and the cut list — the paperwork that goes to the
machine beside the .anc files. `gui/sheet_canvas.py` already draws the sheet
picture the page needs, and `post/job.py` already computes the per-sheet
contents and run quantities.
