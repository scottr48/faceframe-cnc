# Claude Code Prompt — Faceframe Nesting Optimizer + NC Generator

Build a Python desktop application for Eagle Woodworking that reads a cabinet
order spreadsheet, computes faceframe geometry, optimizes sheet nesting
(including placing small frames INSIDE the open interior of larger frames —
the one thing our commercial CAM cannot do), shows the layout in a GUI with
drag-to-override, and generates machine-ready NC code (.anc) matching our
existing Fanuc-style post exactly.

The app must run fully OFFLINE on a Windows shop PC. No internet calls, no
cloud services. Python 3.11+, PySide6 (or Tkinter if PySide is a problem)
for the GUI. Package it so it can be launched with a single script or a
PyInstaller exe.

---

## 1. Reference files (ground truth — do not invent)

The user will place these in a `reference/` folder. Parse them; they are the
canonical spec for the NC post and the spreadsheet format:

- `R620101N.anc`, `R620102N.anc`, `R710101N.anc` — real production NC files
  that ran on the machine. The generated output must match their structure:
  header, footer, tool blocks, modal G-codes, feeds, speeds, Z strategy,
  comment style, CRLF line endings, `%` program wrappers, O-number line.
- `R720101N.anc` — THE KEY REFERENCE: a real sheet where the user manually
  placed two small frames INSIDE two host frames' openings (18×30 inside a
  27×42; 30×12 rotated to 12×30 inside a 24×42). This demonstrates exactly
  how the post must handle nested frames. Replicate its strategy.
- `R730101N.anc` — drawer-frame reference: a real sheet of four drawer
  frames (3DB24 upright, 3DB30 rotated 90°, B30 rotated 90°, B18 upright).
  Validates the §3 opening rules against machine output and proves that
  drawer frames may be ROTATED on the sheet (openings rotate with the
  part). It also shows the finishing convention: T11 through-cuts openings
  leaving ~0.01/side of stock and the 0.200 T12 pass finishes openings to
  exact size — replicate, don't reinvent.
- `7-21-26_Cab_Tec_Order_with_specs.xls` — a real order file for parsing and
  as the primary test case.

Rule zero: where this prompt and the .anc files disagree, the .anc files win.
Never invent post details. If a cut situation arises that the sample files
don't demonstrate, stop and surface it to the user rather than guessing.

---

## 2. Order spreadsheet parsing

Format: legacy .xls (use pandas + xlrd). No header row usable; read by
column index. Relevant columns (0-indexed):

| Col | Meaning |
|---|---|
| 2 | QTY |
| 3 | PART # (e.g. W3330, B30, 3DB24, LS36) |
| 7 | FRAME W (outside width, inches) |
| 8 | FRAME H (outside height, inches) |
| 13,14 | Top drawer face W, H |
| 16,17 | Middle drawer face W, H |
| 19,20 | Bottom drawer face W, H |

Parsing rules:
- Include only rows with QTY > 0 and a numeric FRAME W and H.
- Exclude the "Quantity Total" row.
- Some rows have QTY > 0 but missing frame dimensions (e.g. `SD1212`,
  and `WDC2436` is missing width in the 7-21 file). Do NOT silently guess.
  List these rows in a "needs attention" panel and let the user type the
  missing dimension (for WDC2436 the width is 24 — the part name encodes
  24x36 — but the user confirms).
- Values may contain junk like `?`; a safe float parser is required.

Frame type is inferred:
- Part number starting `3DB` → three-drawer frame.
- Part number starting `B` (B18, B30 …) → base frame (drawer over door).
  (`BBC` is NOT a base-drawer frame — see next line.)
- Everything else (W…, WDC…, LS…, MC…, SB…, BBC…, V…, OVD…) → wall-style
  frame with a single opening. CONFIRMED by the user: besides the drawer
  frames (B and 3DB patterns), every other part family is a plain
  single-opening frame — no false fronts, no extra cross bars.

---

## 3. Faceframe geometry engine

All members (stiles, rails, cross bars) are 1.5" wide. All openings are
routed through; the frame is cut whole from the panel. Opening width is
always FRAME W − 3. Positions below are top-down; "fills remainder" means
the last opening absorbs whatever height is left so the stack closes
exactly to FRAME H.

**Wall frame** — one opening:
- opening = (W−3) × (H−3), inset 1.5 all around.

**Base frame (B…)** — drawer over door:
- 1.5 top rail → 5.0 drawer opening → 1.5 cross bar → door opening
  (fills remainder) → 1.5 bottom rail.
- Example B30 @ 30×34.5: drawer 27×5, door 27×25.

**Three-drawer (3DB…)**:
- 1.5 rail → 5.0 top opening → 1.5 bar → 9.875 middle opening → 1.5 bar →
  bottom opening (fills remainder) → 1.5 rail.
- Example 3DB30 @ 30×30: openings 27×5, 27×9.875, 27×9.125.

Note: drawer FACE sizes in the spreadsheet are the applied fronts (face =
opening + overlays); the ROUTED openings follow the fixed rules above. Do
not derive opening heights from the face columns — use them only to detect
that a line is a drawer frame.

Validation: for every frame, assert the vertical stack sums exactly to
FRAME H and every opening height is > 0. If a frame is too short for its
pattern (negative remainder), flag it in the UI instead of generating
garbage geometry.

---

## 4. Nesting optimizer

Sheet: **49 × 97**, MDF 3/4", loaded face DOWN. (The nominal panel is 48×96;
we run 49×97 stock — keep sheet size a setting.)

Two placement mechanisms, applied together:

### 4a. Footprint packing (what the CAM already does)
2D rectangular bin packing of whole frame footprints (outside W×H) onto
sheets. 90° rotation allowed **for every frame type, including drawer
frames — their openings rotate with the part** (proven in R730101N.anc).
Spacing measured from the production files:
- **Gap between adjacent parts: 0.375"** (edge to edge)
- **Edge cushion is a SOFT preference:** parts MAY sit right at the sheet
  edge when packing requires it (the cut line may ride up to 0.375" outside
  the part edge into the trim margin), but when space allows, leave a
  cushion around the outside edge of parts — default cushion 0.5",
  configurable. The optimizer should treat edge placement as a last resort,
  scoring layouts with cushions higher than layouts that touch the edge.
- Rotation is placement-only: a part may be placed in any orientation, but
  its dimensions must ALWAYS be exactly what the order form specifies.
  Never resize, trim, or alter a frame to make it fit.

### 4b. Frame-inside-frame (the whole reason this app exists)
When a frame's open interior is big enough, place another frame's ENTIRE
footprint inside it. This is the capability our CAM lacks.

Rule (confirmed by the user):
- Host interior opening = (host W − 3) × (host H − 3).
- An inner frame fits if inner outside footprint + **0.375" clearance on
  each side** fits within that opening, i.e.
  `inner_w + 0.75 ≤ host_w − 3` and `inner_h + 0.75 ≤ host_h − 3`,
  with 90° rotation of the inner allowed.
- Only frames with a single large opening (wall frames) can host. A base or
  3-drawer frame's openings are subdivided by cross bars — each individual
  opening may still host if a frame fits inside THAT opening with the same
  clearance rule.
- A host CAN take multiple inners, but **prefer exactly one inner per
  host**. Only place multiples if the user drags them in manually.
- When several inners fit a host, **prefer the smallest** (bigger residual
  gap = better vacuum hold-down of the host web around it).
- Nesting can recurse (an inner's own opening hosting a third frame) only
  if the clearance rule passes at every level; in practice depth 2 is the
  expected max — make deeper recursion a setting, default off.
- Vacuum hold is confirmed OK for parts cut free inside an opening; no tabs
  required.
- **Placement within the host opening: CENTER the inner** (as in
  R720101N.anc, where the user left ~3–5" of web on every side). The 0.375"
  clearance is a hard minimum for validation, not a placement target. If an
  inner barely fits (clearance near minimum), still center it.

**Verified nested-cut strategy (from R720101N.anc — replicate this):**
1. First T11 pass: through-cut ALL openings — host openings AND the inner
   frames' own openings — in one section. The inner's opening is cut while
   its slab is still part of the host's interior waste, held by vacuum.
2. T12 pass: detail/corner cleanup on all openings.
3. Final T11 pass: cut ALL part perimeters (inner perimeters and host
   perimeters), two passes each, tool center 0.1875 outside the part edge.
4. T13 panel cutter section appears first in the file for straight
   separating cuts between adjacent footprints (may be empty when parts
   are isolated). Sheet sequence: T13 → T11 → T12 → T11. T2 roughing was
   used on older files (R620101N); prefer the newer T13-based sequence and
   make the roughing section optional/configurable.

Objective, in priority order:
1. Minimize total sheets.
2. Maximize frame-inside-frame placements (each one recovers a footprint).
3. Prefer identical repeated sheet layouts (see 4c).
4. Prefer larger residual clearance around inners.

For this test order the expected host→inner candidate table (verify the
optimizer reproduces it):

| Host (opening) | Eligible inners |
|---|---|
| W3036 30×36 (27×33) | W3012, W3024, B18, 3DB24 |
| LS36 36×30 (33×27) | W3012, W3024, B18, 3DB24 |
| W2742 27×42 (24×39) | W3012, B18 |
| W2442 24×42 (21×39) | W3012, B18 |
| W2436 24×36 (21×33) | W3012, B18 |
| WDC2436 24×36 (21×33) | W3012, B18 |

### 4c. Sheet uniqueness and quantities
A "unique sheet picture" is a distinct combination of footprint layout AND
contents (which part is where, including which inner sits in which host).
Sheets that are identical get ONE NC file and a run quantity. Partial
(remainder) sheets and mixed-content sheets are their own unique pictures.
The output list is: unique sheet → run quantity, summing to total sheets.

---

## 5. GUI

The end-to-end workflow the app must deliver: the user drops an order .xls
into the app → it extracts the faceframe lines → the user unchecks any
lines they don't want to cut right now → optimize → the app reports how
many sheets are needed → one NC program per unique sheet plus a labeled
PDF page per sheet.

Main window with:
- **Order panel**: parsed lines (part, qty, frame W×H, type), each with a
  **checkbox to include/exclude that line from optimization** — the user
  cuts only the lines they want. Re-optimizing after toggling is instant.
  Plus the "needs attention" list for rows with missing data.
- **Sheet preview**: one sheet at a time, drawn to scale (49×97), parts as
  rectangles with the routed openings drawn inside them so the user can see
  the frame members and where inners sit. **Every part is labeled with its
  part number (e.g. "3DB24")** on the drawing. Navigation across unique
  sheets; each shows its run quantity.
- **Drag-to-override**: the user can drag any part to a new position on the
  sheet, rotate it 90°, move it between sheets, or drag a small frame into
  a host's opening. Live collision/clearance checking against the 0.375
  rules — invalid drops snap back with the violated rule shown. After
  edits, the affected sheets become new unique pictures and NC regenerates.
- **Summary panel**: total frames, total sheets needed (the headline
  answer), sheets saved vs no-inside-nesting baseline, list of unique
  sheets with quantities.
- **Generate button**: writes one .anc per unique sheet (identical sheets
  share one program; the summary and PDF state the run quantity — add a
  toggle to emit one file per physical sheet instead if the user prefers)
  plus a printable PDF report: **one page per unique sheet, drawn to
  scale, every part labeled with its part number (e.g. "3DB24"), with the
  run quantity prominent in the header.**

---

## 6. NC generation (.anc post)

Match the reference files exactly. Structure per file:

```
%
O00nn (FILENAME)
(CREATED ON DD MMM YY - HH:MM)
(MATERIAL: MDF 3/4 )
(LOAD: Material face DOWN)
G0 G20 G91 G28 Z0 M15
G90 G40 M22
M88 B0
M89 B0
G08 P1
M25
  ... tool sections ...
M22
G91 G28 Z0 M15
G90 H0 M25
M88 B0
M89 B0
G91 G28
G90 X24. Y96.
M59
M07
G08 P0
M30
%
```

Units inch (G20), absolute (G90), work offset G54, origin at sheet
bottom-left, X across the 49" width, Y along the 97" length. CRLF endings.

Tools (from the reference files — parse their blocks for exact patterns):

| Tool | Description | Dia | Role |
|---|---|---|---|
| T2  | 3/8 down shear | 0.375 | Opening roughing: groove around each opening in many shallow Z passes (Z from ~0.7367 stepping ~0.0133/pass), S18000, F300 entry / F800 cut |
| T13 | 3/8 panel cutter | 0.6299 | Straight separating cuts between adjacent parts and at sheet edges |
| T11 | 3/8 compression 1.375 long | 0.375 | Through cuts: openings and part perimeters to Z0.15, S16700, F150 entry / F545 cut; perimeter tool center rides 0.1875 OUTSIDE the part edge |
| T12 | 0.200 downshear | 0.200 | Detail/corner cleanup pass |

Observed sequences: R620101N uses T2 → T11 → T12 → T11; R710101N uses
T13 → T11 → T12 → T11. Implement the post by REPLICATING the reference
blocks: extract each tool section's motion grammar (entry move, lead-in,
rectangle path direction, retract pattern, M-codes like M59/M13/G43 H#)
from the sample files programmatically and emit the same grammar with new
coordinates. Where the two files differ, prefer the newer R710101N.

File naming: CONFIRMED — same format as the existing files: `R` + digits +
`N` + `.anc` (e.g. `R730101N.anc`: R + job/date digits + 2-digit sheet
index + N). Generated files must follow this exact pattern, with the digit
prefix configurable per job and the sheet index auto-incrementing (01, 02,
…). The O-number line inside the file numbers sequentially (O0001, O0002…)
as in the references.

**Safety requirements:**
- Every generated file carries a comment banner identifying it as generated
  by this app, with the sheet's content list.
- A "dry-run mode" toggle that emits the file with all cutting Z depths
  raised above the stock (air-cut) for first-article verification.
- A built-in verifier that re-parses each generated file and checks: all
  moves within sheet bounds + allowed overhang, no cut segment enters
  another part's footprint, Z depths within stock+tool limits, and the
  header/footer byte-match the templates. Refuse to write a file that
  fails verification.

---

## 7. Milestones (build in this order, test each)

1. Spreadsheet parser + geometry engine, with unit tests using the exact
   examples in §3 (B30 → 27×5 + 27×25; 3DB30 → 27×5, 27×9.875, 27×9.125),
   plus a cross-check against R730101N.anc: parse its cut coordinates and
   assert the decoded openings match the geometry engine's output for
   3DB24 (21 × 5 / 9.875 / 9.125), B30 rotated, and B18 (15 × 5 / 20.5).
2. Optimizer without inside-nesting; verify spacing rules and sheet counts.
3. Frame-inside-frame placement; verify the candidate table in §4b against
   the 7-21-26 order.
4. GUI preview + drag-to-override with clearance validation.
5. NC post: first reproduce R710101N's sheet from its own layout data and
   diff against the real file (structure-level match, coordinates within
   0.001). Then do the same for R720101N — the nested-frames sheet — since
   that is the case the app exists to generate. Only then generate for
   optimized sheets.
6. PDF cut-sheet report.

Acceptance test: load `7-21-26_Cab_Tec_Order_with_specs.xls`, resolve the
WDC2436 width (24), exclude SD1212 (no frame dims) after prompting, run the
optimizer, and produce: sheet count with and without inside-nesting (the
delta is the app's value), the unique-sheet list with quantities, one .anc
per unique sheet passing the verifier, and the PDF report.

## 8. Z-limit safety (CRITICAL — machine damage risk)

The optimizer and post must enforce hard minimum and maximum Z depths to
prevent:
- **Too deep (Z minimum — spoilboard strike)**: if the cutting Z goes below
  the material depth plus a tiny relief cut into the spoilboard (typically
  Z0.55 in the reference files is aimed at a target depth of ~0.75" into
  the 3/4" stock, leaving ~0.2" spoilboard penetration). Set a configurable
  Z minimum (default from reference files; user may adjust per machine
  calibration). Any cutting move that would go below this Z is rejected —
  the verifier flags it as an error and refuses to write the file.
- **Too high (Z maximum — overtravel)**: the machine has a physical Z limit
  (typically Z2.5 or higher is safe rapid/travel height in the reference
  files). Set a configurable Z maximum (default Z2.5). Any move to Z above
  this triggers an error.
- **Application**: every tool block in the generated NC must respect these
  limits. Do not allow the user to override — these are machine protection,
  not preferences. If the reference files use Z0.55 as the deepest cut and
  Z2.5 as the highest travel, embed those as the factory defaults with a
  UI warning that changing them risks hardware damage.

## 9. Things NOT to do



- Do not use the drawer FACE columns to size routed openings.
- Do not place two footprints closer than the configured gap, ever —
  including an inner frame to its host's opening edge.
- Do not emit NC for any geometry the verifier flags.
- Do not invent G/M codes, feeds, or Z strategies not present in the
  reference files; when unsure, ask the user and stop.
- Do not allow NC output if any Z depth violates the configured min/max
  limits — treat Z violations as hard failures, not warnings.

---

## Amendments (corrections from Scott — these override the sections above)

- **2026-08-03 — WDC frames**: WDC is a special wall-style frame. WDC2436 is
  **18" wide × 36" tall** (the part name encodes the diagonal-corner CABINET
  size 24×36, not the frame size — §2's "the width is 24" is wrong). Top and
  bottom rails are 1.5" as usual, but the **stiles are 2" wide**, so the
  single opening is (W − 4) × (H − 3) = 14 × 33 for WDC2436. This also
  supersedes the §4b host-table row for WDC2436: with a 14×33 opening, a
  rotated W3012 (12×30) still fits as an inner, but B18 (18 wide) does not.
