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

## State: ALL milestones (1-6) complete, reviewed and committed

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
  2026-08-04 review fix: the packer now enforces the WDC hard edge rule
  itself (`_edge_inset` — reserved rectangle stays `part_gap` off the
  slot-axis sheet edges, front/back charged constructively in
  `_select_shelves`, side rows corrected by one retry), so it can no longer
  emit a layout `validate_layouts` refuses; impossible WDC sizes are
  refused up front by `_normalize_demand` with the T17 reach in the
  message. Non-WDC sheets are laid out bit-identically to before; the
  footprint-only baseline is now 18 unique pictures (was 19).

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
  splitting and re-grouping; 2026-08-04 review fix: `set_included`/
  `set_all_included`/`resolve_row` now invalidate the layout exactly like
  `edit_row` (a layout built from the pre-change cut list must never reach
  Generate), and `MainWindow._on_order_changed` calls `refresh()` so button
  states can never go stale (the "Optimize grayed out and stayed gray"
  owner report); row editing — `Session.edit_row`/`revert_row`
  change a line's qty/dimensions with the same trial-then-commit discipline,
  double-click a row or "Edit..." opens `EditRowDialog` with an explicit
  "Save changes" step and a live before/after summary, and any successful
  edit invalidates the current layout so a stale one can never reach
  Generate). Qt widgets are thin. Launch:
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
  - **2026-08-04 review hardening** (external gpt-5.6-sol review, all
    findings verified before fixing):
    - `verifier.expected_work` — build_job hands verify() a manifest of
      every cut the sheet OWES (derived from the layout + geometry.py +
      model.py, deliberately NOT from from_layout/generator — an AST test
      forbids the imports), with depth-aware recovery: missing through
      passes, missing openings, missing T13/T17 features and extra cuts
      are now `missing-cut`/`extra-cut` refusals. Dry runs are judged
      against their own lifted manifest. 7-21 order: 1008 owed cuts per
      form, all matched.
    - Feed/speed check (`feed`/`spindle-speed` violations): every F word
      judged modally against the tool's entry/cut feeds, every S word and
      every M13 against the tool's RPM, all from the PostConfig in hand.
      T11 legitimately runs two cut feeds (545 openings / 498.2 perimeter).
    - `write_job` — atomic writes (`.partial` + os.replace; a failed write
      can't truncate the file already there) and stale-file quarantine:
      files matching this job's exact name pattern that this run did not
      write (shorter re-run leftovers, refused sheets' old files, orphaned
      partials) are MOVED to `superseded/<stamp>/`, never deleted, listed
      on `JobResult.superseded`; a failed move is a loud
      `quarantine_problems` entry and the GUI headline says the folder is
      not safe for the machine yet.
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

- `faceframe_cnc/report/` — Milestone 6 PDF cut-sheet report. `pdf.py` is a
  stdlib-only, byte-deterministic PDF 1.4 writer (real Helvetica AFM
  metrics); `cutsheet.py` composes a cover page (headline tiles,
  unique-sheet table, run-quantity sum cross-check) plus one page per
  unique sheet: boxed RUN QTY, layout to scale with openings/nested frames/
  T17 slot centrelines (dashed, on WDC parts), labels, and a cut list.
  Refused sheets get a marked page; dry runs marked on every page.
  Generate writes `R<prefix>_report.pdf` beside the .anc files (default-on
  checkbox; a report failure never blocks the NC programs).
  `tests/test_report.py` parses the PDF back and asserts structure,
  determinism and drawn coordinates.

## 2026-08-04 session: external review + six fixes (this commit)

Scott asked for a "Codex Sol" high-effort review (OpenAI Codex CLI,
gpt-5.6-sol, read-only sandbox) of the whole tree, plus a fix for an
owner-reported bug ("edited some lines then the Optimize button grayed
out"). The review returned five findings, every one verified against the
code (two reproduced with scripts) before fixing; the sixth fix is the
feed-word follow-up, owner-approved the same day. All six are in this
commit, each written by a subagent and reviewed:

1. include/resolve didn't invalidate the layout → stale NC reachable from
   Generate (gui/session.py) — CRITICAL.
2. bare open(path,"w") over production filenames → atomic writes + stale
   quarantine (post/job.py) — CRITICAL.
3. verifier blind to MISSING cuts → expected-work manifest
   (post/verifier.py, job.py) — MAJOR.
4. button states recomputed only in refresh(), which order changes never
   called → the grayed-Optimize trap (gui/main_window.py) — MAJOR.
5. WDC stile ends packable 0.42" from the sheet edge vs the 0.875 hard
   rule → packer enforces the edge rule (nesting.py) — MAJOR.
6. F/S words unchecked → per-tool feed/speed verification
   (post/verifier.py) — the RESUME follow-up, now closed.

Tests 515 → 602, all green; 7-21 order still 41 sheets / 17 unique, zero
refusals, production and dry-run; reference round-trips still byte-exact.

## 2026-08-04 later session: full-tree double review + fix-everything pass

Scott asked for a full review of the whole tree, then a Codex Sol pass
framed adversarially ("tell us why we made this incorrectly"), then fixes
for everything. Five internal review agents (one per subsystem) + Codex
ran; every CRITICAL/MAJOR claim was verified against the code before being
accepted (several Codex claims were re-verified by hand). The two reviews
overlapped on some findings and each caught things the other missed — the
internal review found the worst one (WDC parser), Codex found the Fanuc
blind spots. Five coder agents (Sonnet routine / Opus complex, disjoint
file ownership) implemented all fixes; every diff was reviewed before
acceptance. **Tests 602 → 766, all green.**

Highlights (each with a test that fails on the old code):

- **CRITICAL, parser**: a WDC row with BOTH dims present skipped the
  contradiction check, and the shop template prefills the CABINET width —
  the real 7-7 order parsed WDC2436 qty 30 as a READY 24×36 (20×33
  opening). Now: `check_wdc_dimensions` auto-corrects the exact
  template-prefill signature with a provenance note (visible-proof
  pattern); any other mismatch → needs_attention. 7-7 now parses 18×36,
  empty needs-attention.
- **Verifier hardening** (5 new violation kinds): `rapid` (no rapid
  travels/descends below stock top; post-G28 unknown-Z exempt, and
  `g-mode` stops manufacturing that state), `cut-order` (onion-skin
  before through, inner's through before host's, through pass last —
  references satisfy all three, nothing weakened), `tool-comp` (G43 H
  must match the active tool), `spindle-start` (M13 required before the
  first feed move), `g-mode` (G90/G91/G28 only on byte-pinned template
  lines). Also `_check_config` now validates approach_z/rapid_z vs
  stock_top_z and cross-validates diameter_comment vs diameter.
- **Short-part false refusals fixed both ends**: verifier models the
  perimeter lead-in ramp's actual Z profile (only at-depth segments are
  material checks); generator falls back through entry sides when the
  default lead-in leaves sheet+0.375. All 34 generated 7-21 forms
  byte-identical to before.
- **`_owner_of` self-skip CLOSED** (the old recorded follow-up): the
  owner exemption no longer applies to through cuts; a nested inner's
  through-cut overshooting into its host went 0 → 2 violations. Side
  effect: RFK0101N (foreign file, T16, already failing header) reports
  16 findings instead of 8 — artefact, accepted deliberately.
- **Job lifecycle**: per-run-unique `.partial-<pid>-<ns>` temp names;
  two-phase publish (write+fsync+read-back ALL partials, then one tight
  rename loop); an existing program is ALWAYS quarantined to
  superseded/<stamp>/ before being replaced — a prior order's programs
  can no longer be destroyed, and an unpreservable file blocks that
  sheet's publish. O-number overflow validated up front.
- **GUI**: `Session.set_settings` is now the only settings path, with
  edit_row-style invalidation (both escape paths that left a stale
  layout generate-able under new settings are dead; decline-the-prompt
  applies nothing). Unmillable openings (< 0.395, from the post table,
  read-only) are refused BY NAME at Optimize, not at Generate.
  Qty-problem rows (fractional qty like 2.9 — parser no longer floors)
  get a Quantity box in the resolve editor; EditRowDialog's QSpinBox was
  silently flooring 2.9 → 2 (found+fixed). No-op clicks no longer mark
  the layout edited; stale-selection keyboard crash fixed; resolve
  editor no longer retargets on panel reload; file dialog is .xls-only
  with a save-as hint for .xlsx.
- **Parser robustness**: safe_float rejects non-finite ("1e999"/"inf"/
  "nan"); fractional/non-finite qty → needs_attention (blank/junk still
  skipped); drawer-base families (2DB/4DB/MICRO3DB → new
  `FrameType.UNSUPPORTED_DRAWER_BASE`, geometry refuses) and
  B-accessories (BSK/BFD/BPP/BES/BF, verified never ordered in the
  reference files) → needs_attention instead of silently wrong frames.
- **Report**: dry-run banner on cover continuation pages (path now
  test-exercised); part numbers truncated clear of the count column;
  refused-list wrapped+capped; `_dim` prints exact 32nds at any
  magnitude; truthful partial-failure banner ("N OF M FILES FAILED");
  text_centered_in glyph-band math fixed; cut-list overflow opens a real
  continuation page; sheet_reports cross-checks outcome contents vs
  layout (stale job + fresh result now raises); atomic PDF write.
- **Nesting**: off-grid sheet widths refuse cleanly up front (shared
  `_quantize_capacity`/`_width_units` so gate and knapsack can't drift;
  capacity rounding deliberately NOT loosened); non-finite qty →
  NestingError. Default-sheet output byte-identical.

Deliberately NOT done (design decisions, not bugs): the T13 shallow-cut
swept-width waiver stays (mirrors the reference files / current
production); Codex's packaging complaints (installer, README, settings
location); instant re-optimize on checkbox toggle; T2 support.

## Where the previous session left off (2026-08-03/04)

Session log, in order — commits `91757dd` (Milestone 5 complete),
`ec2da9a` (Milestone 6 PDF report), `b7212d6` (part-gap floor + WDC
auto-resolution), then row editing (committed at wrap-up; see git log):

- **Row editing** (Scott's request): `Session.edit_row`/`revert_row` +
  `EditRowDialog`; provenance originals kept on `OrderRow`, edited rows
  amber with note tooltip, "Save changes" disabled until something differs
  with a live before/after summary. Review caught and fixed two corner
  bugs: completing a missing dim while changing the present one no longer
  drops the second change, and an untouched 0.001 placeholder spin value is
  never sent as a real width. **515 tests, OK** at wrap-up.
- **Owner-reported bugs fixed** (b7212d6): stale persisted part_gap 0.375
  caused 7-8 refused sheets at Generate (verifier foreign-cut, correctly)
  → hard floor + migration; WDC2436 prompted for width on every load →
  parser auto-derives from the part name (contradictions still prompt).
  The 7-21 order now loads with an EMPTY needs-attention list.
- **WDC trust displays** (Scott: "how do I know it has the 2 inch stiles
  and the special T17 routing?"): order-panel "WDC frames — what the
  machine does" fact box and PDF slot centrelines + cut-list note, every
  number derived from geometry.py constants. Pattern to keep (see memory):
  auto-apply derived values AND show visible proof — no silent magic, no
  bare prompts.
- **Dev tooling**: PyMuPDF is installed in `.venv` (dev-only, NOT an app
  dependency) so PDF pages can be rasterized to PNG and inspected:
  `python -c "import pymupdf; d=pymupdf.open('R7201_report.pdf');
  d[0].get_pixmap(dpi=130).save('page1.png')"`. A desktop shortcut
  "Faceframe Optimizer" (pythonw -m faceframe_cnc.gui, workdir this repo)
  launches whatever code is checked out — no reinstall step ever.
- **One session at a time in this tree.** An earlier session collided with
  a leftover coder agent from a disconnected session editing the same
  files concurrently. It merged cleanly that time; do not count on it.

### Decisions made (Scott, 2026-08-03)

- **WDC end clearance stays** even though it costs 1 sheet on the 7-21
  order (41 vs 40): "the extra sheet is worth it for the padding."
  Do not re-raise trading it back for shallow T17 nicks in neighbours.
- **Part gap 0.455 is a hard floor**; **WDC width is derived, not
  prompted**; derived values must always come with visible proof.

### Known follow-ups (recorded, non-blocking)

- ~~The verifier checks no `F` (feed) words~~ — CLOSED 2026-08-04 (fix 6
  above; `feed`/`spindle-speed` violation kinds, 24 tamper tests).
- ~~`verifier._owner_of` self-skip~~ — CLOSED 2026-08-04 later session
  (failing test first; owner exemption no longer applies to through cuts).
- ~~The cover table's continuation page is unexercised~~ — CLOSED
  2026-08-04 later session (dry-run banner bug found on it and fixed;
  45-picture test fixture exercises it).
- The PDF has been eyeballed rendered at screen resolution (cover, plain,
  nested and WDC pages all correct); nobody has checked a PRINTED page yet.
- `report/cutsheet.py` and the .anc job stamp "now" independently when no
  `created` is injected — they can differ across a minute boundary.
- Qty-problem needs-attention rows resolve through the GUI's new Quantity
  box; the underlying `resolve()` requires a whole qty for them. A
  spreadsheet fix is still the cleaner path for a shop that keeps the .xls
  as the record.
- Drawer-base families (2DB/4DB/MICRO3DB) are refused as unsupported, not
  cut wrong. If the shop ever orders one, the real fix is implementing
  their cross-bar layouts in geometry.py (like THREE_DRAWER).

## Next

No milestone is open — the spec is fully delivered, and BOTH 2026-08-04
review passes (the six-fix morning session and the later full-tree
double review) are fixed and verified. 766 tests green. Remaining
candidates, Scott's call: a printed-page check of the PDF report,
implementing the 2DB/4DB/MICRO3DB drawer-base layouts if the shop ever
orders one, Codex's packaging/deployment wishlist (installer, README,
settings location), or whatever the shop floor turns up next.
