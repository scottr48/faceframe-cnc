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

## 2026-08-05 session: 3D machine-cut simulation (all 5 milestones)

Spec: `CLAUDE_CODE_PROMPT_Machine_Simulation.md` (in the repo root) — a
Cabinet-Vision-style animated playback of the sheet on screen, tuned to cut
order and error legibility.  Scott's decisions, made up front: motion source
is an **emitter refactor** (structured records, text rendered FROM them —
byte-exactness guardrail held throughout), the sim opens in a **separate
non-modal window**, and three owner must-haves: a prominent current-tool
field, free orbit/pan/zoom camera, and a playback speed control.  Qt3D
verified present in PySide6 6.11.1 on Py 3.14 — no new dependency.
**Tests 766 → 1046, all green; reference .anc round-trips still byte-exact.**
Five milestones, each written by an Opus subagent and reviewed:

- **M1 `post/motion.py` + generator refactor (+25 tests)**: `_Emitter` now
  appends `Event`s — each one rendered line PAIRED with the `Motion` it
  commands (kind rapid/plunge/feed/retract, from/to XYZ, tool, feed, section,
  FeatureRef, depth pass, `line_index`), built from the same numbers at the
  same moment.  `generate()` is a join over the stream; `emit()` /
  `generate_motions()` are the new API.  Old-vs-new output byte-compared on
  reference AND optimizer sheets: identical.  Bonus: fixed a backwards
  docstring sentence (the DEEPER T17 pass overruns FURTHER — RFK0101N
  Y37.3438 shallow / Y37.4375 deep); code was always right, prose wasn't.
- **M2 `sim/` package (+49)**: headless timeline (steps + *cut occurrences* —
  a perimeter ref is two, onion-skin and through; a WDC slot is its two
  bites; labels derived live from the post table, e.g. "T11 perimeter pass 1
  of 2 (onion skin 0.06 thick) — WDC2436"), material state (grooves /
  openings / slot bites cut, *skinned* vs *freed*), `SimController` cursor
  (step/cut/section, reversible, clamped), `step_for_line()` for M4.  AST
  test bans Qt and wall-clock from the package.
- **M3 `gui/sim3d/` (+70)**: pure `viewmodel.py` (tool field text from
  `header_comment`, reveal geometry via the generator's own
  `groove_segment`/`wdc_slot_segment`, feed-true animation maths), GL-free
  `scene.py` (spoilboard/stock slabs, tinted part faces in sheet_canvas's
  colours, V-slot as two angled flanks, freed part LIFTS 0.30 with edge
  highlight, per-tool bit meshes — cone for T17), thin `window.py` (cut list,
  transport, scrub, speed 0.25x–20x default 4x, the big bold tool field).
  Viewport injection seam so every test runs without GL.  Demo:
  `python -m faceframe_cnc.gui.sim3d --demo wdc|nested [--play]`.
- **M4 findings (+101)**: `sim/findings.py` maps `verify()`'s violations onto
  the timeline (line → step → cut → part; unmappable → global) — total,
  faithful, verbatim.  Red = a Violation, nothing else: tinted features, red
  bars on flagged moves, red bit while executing one; clean sheet renders
  pixel-identical to M3.  Informational overlays (default OFF): WDC cone
  reach (drawn from `wdc_slot_sweep`, the enforcement function itself),
  lead-in envelopes, sheet+overhang fence.  `SheetPlanError` gained optional
  `part_number`/`box` attributes (messages byte-identical) so `RefusalView`
  shows a refused sheet in 3D with the part outlined.  Four crafted bad
  sheets prove marks correspond 1:1 with the authority.
- **M5 integration (+35)**: `Session.simulation_inputs(i)` — same
  `plan_sheet` wiring, same post table, and the SAME `expected_work` manifest
  as `build_job`, so the sim's verdict is Generate's verdict; gated by
  `generate_blocker()` reused verbatim (one notion of stale).  "Simulate cut"
  button + toolbar action beside Prev/Next, state recomputed only in
  `refresh()`.  One sim window at a time, parentless non-modal, reference
  held, closed with the main window.  Refusals open `RefusalView`; every
  handler guarded.  `--self-test-sim` runs the whole path offscreen
  (viewport hook → None).

### Simulation follow-ups (recorded, non-blocking)

- **Nobody has eyeballed the render aesthetics** — tests prove structure, not
  taste.  Ten seconds: `--demo wdc --play`, or Simulate cut on a real order.
- **Shop-PC smoke test**: Qt3D needs working GL drivers; the dev box renders
  fine, the shop PC is unproven.  Failure is graceful (a message box; the
  sheet still plans/verifies) but should be checked before relying on it.
- The reveal list is recomputed every 16 ms tick — fine at demo size; cache
  per completed-cut count if a busy 7-21 sheet ever feels sluggish.
- The sim window is a deliberate snapshot: a later order edit does not close
  it (title names the program).  No keyboard shortcuts on Simulate (every
  existing shortcut is whole-job; a per-sheet one would fire on whatever is
  showing).  Cut-list labels carry an em dash — Qt renders it; a cp1252
  console print shows a replacement char (cosmetic, demo/self-test only).

## 2026-08-05 later session: T13 groove clamp + tabbed holding (job R0805)

Spec: `CLAUDE_CODE_PROMPT_Tabs_and_Groove_Clamp.md` (repo root). Two
production failures on job R0805 (sheet `R080501N.anc`, 1×W3330 rotated +
1×WDC2436): the W3330's T13 stile grooves overran 0.375 past the part ends
and the 0.6299 cutter took two bites 0.235" into the WDC 0.455" away (and ran
0.42 past the sheet edge); and both frames broke during perimeter cutout
because the opening dropouts were fully freed before the perimeter was
touched. Scott ratified: clamp all T13 grooves inside the part (over a
nesting-clearance rule), and hold everything with tabs released by a final
slow T12 pass. **Tests 1149 → 1216 across the session (from 1046), all green
except the six golden round-trip methods awaiting the §5 re-blessing below.**
Each milestone written by an Opus subagent and reviewed; nothing committed.

- **M1 — groove clamp + verifier gap.** Why the verifier passed R0805: the
  foreign-cut check judged shallow (not-through) cuts at the tool CENTRE
  only — the deliberate 2026-08-04 "shallow-cut waiver" — and the groove's
  centre never enters the neighbour, only its swept width does. Waiver
  closed: every in-material cut is judged on swept width against foreign
  parts (own part stays exempt; that is what lets a groove cut its own
  part). `groove_segment` clamps stile-groove endpoints to one T13 radius
  inside the part ends (`PanelSpec.end_inset = 0.0` = flush, Scott's
  ratified choice, the single place to adjust); rail grooves and WDC/T17
  byte-identical. reconstruct matches BOTH shapes (clamped + legacy) so the
  reference files still read. **R710101N and R730101N are now REFUSED — 5
  pinned findings each (`LEGACY_GROOVE_FOREIGN_CUTS`): the shop's own CAM
  made the identical divot cut on those sheets.** Scott ratified the
  grandfathering. Fixture: `tests/test_r0805_regression.py` + frozen
  `tests/data/r0805_old_emission.anc` (refused forever); nothing catches a
  shallow cut running off the sheet edge (recorded open; moot post-clamp).
- **M2a — onion-skin perimeter pass removed** (Scott: "don't need it
  anymore" — tabs hold the parts now). Generated sheets run ONE perimeter
  pass, the through pass (`generated_post_passes`/`post_config_for` in
  from_layout — the only spot); the measured two-pass dialect stays in
  `default_config` for reading references. Through pass now cuts 0.756 deep
  vs the skin's 0.69 (~10% more chip load). The verifier backstop for the
  part gap at exactly 0.375 is half-gone (through pass sweeps exactly
  0.375 — tangent; the lead-in ramp still refuses entry-side neighbours at
  0.3938); the enforcement is `nesting.MIN_PART_GAP = 0.455`. NOTE:
  `validate_layouts` does not itself enforce the floor (validates against
  the config's own gap) — the app can't produce a sub-floor layout, but a
  hand-built one via `Session.set_result` isn't caught there.
- **M2b — tab model** (`post/tabs.py`, pure/deterministic; `TabSpec` on
  PostConfig: top_z 0.25, length 0.75, corner clearance 2.0, max_gap 10.0 —
  ratified policy, not measurement). Zones live on the FINISHED profile
  (side + midpoint-relative centre) so one tab block spans both opening
  kerfs; placement per side: symmetric, ≤10" gaps (2 on 14", 4 on 30-36"),
  ≥2" corner clearance including ramps, lead-in span excluded (derived
  ≈4.01", never hardcoded), relocate-not-shrink; documented fallback chain
  for degenerate sides (fewer → one centred → clearance yields → zero).
  Emitter lifts every pass below 0.25 over every zone (climb+traverse at
  modal cut feed, descent at entry feed — the loop's own ramp grammar, no
  new F values).
- **M3 — release section + wiring + verifier + sim + report.**
  `ReleaseSpec` (cut 150. / plunge 50., Scott-approved ~50% of T12 detail;
  overlap 0.1 = proposal pending Scott). Sections: T13 → T17 → openings →
  detail → perimeter → **T12 release, always last**; per tab: rapid into
  open kerf, plunge F50, one straight cut F150 through the tab
  (0.75 + both ramps + overlap), retract. Release path FLUSH with the
  finished profile (openings: the T12 detail path; perimeters: part edge
  GROWN by the T12 radius — never the T11 centreline, which would leave a
  0.09 rib). Order: opening tabs, then perimeters, inners-before-hosts.
  Verifier `hold` invariant re-derived independently (bridges = gaps in the
  severed boundary; every bridge released exactly once, flush, at the
  release feeds; freed-early/never-released/centreline/wrong-feed refusals;
  too-small bridge reads as NO bridge — safe direction). reconstruct reads
  tabbed programs (and tabbed output round-trips byte-exact — pinned).
  Sim: through pass = skinned, release frees (`RevealKind.BRIDGE` draws
  standing tabs). Report: one-line tab-held note per sheet. 7-21 order: 41
  sheets / 17 unique, zero refusals, production and dry-run, 3416 tabs.
- **M4 — goldens, SIGNED OFF.** `reference/goldens/R71/72/73...anc`
  regenerated; annotated diff in `docs/2026-08-05_golden_reblessing.md`
  (17/17/16 lines, ALL T13 stile-groove endpoints moved by exactly 0.68995
  = 0.375 overrun + 0.31495 radius, plus the T11 section head in R71/R72
  restating the last T13 point; line counts identical, everything else
  byte-identical; tabs/skin/release deliberately absent — round-trips
  regenerate the file's own pre-amendment plan). Scott signed off
  2026-08-05; the six round-trip tests now compare against the goldens via
  `golden()` helpers in test_post/test_motion (reconstruction still starts
  from the measured originals). Suite fully green at that point: 1216.
- **T11 max bite (Scott, 2026-08-05, after the sign-off): the 3/8
  compression bit takes at most 0.4" of material per pass** on generated
  sheets, to reduce load. Equal bites (n = ceil(depth/0.4)): perimeter =
  Z0.372 + Z-0.006 (2 x 0.378), each opening = Z0.45 + Z0.15 (2 x 0.30),
  emitted back to back like the T17 bites. `ToolSpec.max_bite` (None in the
  measured table) + `T11_MAX_BITE = 0.4` applied in `post_config_for`;
  `PostConfig.openings_pass` became `openings_passes` (tuple, mirrors
  perimeter_passes). The intermediate perimeter pass is the measured skin
  spec at Z0.372 (offset 0.1895 spring stock, through pass finish-shaves) —
  so a 0.375 part gap is again refused from every direction. New `max-bite`
  violation kind: configured ladder validated AND the file's actual ladder
  re-derived in file order (dropped rung / deep-first refused). Shallow
  rungs sit above the 0.25 tab top: no lifts, no hold-refusal, tab
  placement identical (3416 zones). References/goldens byte-untouched
  (their plans carry the measured depths). Addendum recorded in the
  re-blessing doc. **Suite: 1246 tests, fully green.**

### Open items from this session

- Ratifications Scott has NOT explicitly seen: the OPENINGS split under the
  T11 rule (his message named the perimeter; the rule as stated is
  per-tool, and the opening pass took 0.60 — if he meant perimeter-only,
  the fix is declaring the limit per-section instead of on the tool); equal
  bites (0.378+0.378) vs max-first (0.4+0.356); release `overlap = 0.1`;
  the degenerate-side tab fallback chain; a 12" ENTRY side (e.g. W3012's)
  holds ONE relaxed tab (min-2 + 2" clearance + 8.4" lead-in don't fit);
  sim shows one release occurrence per profile; rung cut-list wording
  ("0.378 deep, 0.372 left").
- `validate_layouts` doesn't enforce MIN_PART_GAP itself (see M2a note) —
  mostly moot again now that the 0.1895-offset intermediate pass restores
  the verifier backstop at 0.375.
- Cut the R0805 job again for real: acceptance is the sheet coming off the
  machine whole, with no divots.
- Nothing committed — Scott commits when he says so.

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

No milestone is open — the original spec AND the 3D simulation spec are
fully delivered. **1046 tests green**, everything uncommitted as of the
2026-08-05 session wrap (Scott commits when he says so). Remaining
candidates, Scott's call: eyeball the 3D render + smoke-test Qt3D on the
shop PC (see simulation follow-ups above), a printed-page check of the PDF
report, implementing the 2DB/4DB/MICRO3DB drawer-base layouts if the shop
ever orders one, Codex's packaging/deployment wishlist (installer, README,
settings location), or whatever the shop floor turns up next.
