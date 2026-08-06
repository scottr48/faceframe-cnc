# Claude Code Prompt — T13 Groove Clamping + Tabbed Part Holding (Faceframe Optimizer)

Two production failures came off the machine on **05 AUG 26** (job **R0805**,
sheet `R080501N.anc`, 1×W3330 + 1×WDC2436 on one 49×97 sheet). Both are
generator behavior, both were confirmed by reading the emitted `.anc`
line-by-line, and both fixes below were decided by Scott on 2026-08-05:

1. **T13 stile grooves overrun past the part edge and cut into the
   neighboring part.** Fix: **clamp all T13 grooves so the cut never leaves
   the part** (Scott chose clamping over a nesting-clearance rule), and close
   the verifier gap that let this sheet through.
2. **Parts break during perimeter cutout** because the opening dropout is
   fully freed before the perimeter is cut, leaving a thin unsupported MDF
   ring that vibrates and snaps. Fix: **hold everything with tabs** —
   dropout-to-frame and frame-to-skeleton — released by a **final slow T12
   pass over the tab zones only**, as the last machining section.

Read `RESUME.md` and `docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md` (the
original build spec, including its Amendments) first, plus the simulation
prompt if that feature has landed. Every constant, tool, feed, Z level and
coordinate convention already lives in the codebase and must be reused, never
re-derived.

**Rule zero, amended:** the reference `.anc` files and the measured post
tables in `faceframe_cnc/post/model.py` remain the authority for every fact
they encode — tool geometry, feeds, Z levels, insets, lead-ins. **But this
prompt deliberately changes two emitted behaviors** (groove overrun, and the
holding/release strategy). Those changes are ratified amendments approved by
Scott, not drift. Treat this document as the spec for the *new* behavior and
the reference files as the spec for everything else. See §5 for what this
means for the byte-exact round-trip tests.

---

## 0. Working style (per Scott — do not deviate)

- **Fable does not write code.** Delegate to Sonnet (routine) / Opus
  (complex) subagents, review their output, accept or send back fixes. Light
  fixes Fable may do directly.
- **Check in with Scott after each milestone.** Only commit when asked.
- **One session at a time in this tree.** No concurrent coder agents editing
  the same files.
- All logic in headless, testable modules; Qt widgets stay thin.
- Single source of truth for each fact; an **independent** re-check wherever
  safety is involved; the **visible-proof** pattern (show the operator *why*).

---

## 1. Field evidence (from `R080501N.anc`, 05 AUG 26 — verify these against the code before building)

Sheet layout (part edges recovered from the T11 through pass, tool radius
0.1875):

- **WDC2436** (18 × 36): X 0.2725–18.2725, Y 1.42–37.42
- **W3330** (30 wide × 33 tall as placed): X 18.7275–48.7275, Y 1.00–34.00
- **Edge-to-edge gap between the two parts: 0.455"**

### Failure 1 — groove intrusion

The W3330's stile grooves (horizontal in machine coords because the part is
rotated) run at Y 1.5625 and Y 33.4375, tool T13 × 0.6299, from **X 18.3525
to X 49.1025** (centerline). Both endpoints overrun the part edges by
**exactly 0.375** at the centerline — the `overhang` constant. Add the tool
radius (0.315) and the *cut* reaches **0.690" past the part edge** — but the
neighbor is only 0.455" away:

- Left end: cut edge reaches X 18.0376 — **0.235" into the WDC's right
  stile, 0.20 deep** — the two half-round divots in the shop photos.
- Right end: cut edge reaches X 49.4175 — **0.42" past the 49" sheet edge**.

Contrast with the grooves that behaved: the WDC's own rail grooves (X 0.835
– 17.71) stop at the stile lines and never leave the part, and the W3330's
rail grooves (X 19.665 / 47.79, Y 1.5625 – 33.4375) span exactly between the
stile-groove centerlines. **Only the stile grooves overrun** (they run out
through the rail ends, ±0.375 past the part). That overrun matches the
reference `.anc` behavior — it was correct reverse-engineering — but in a
tight nest it is a foreign cut, and Scott has decided it goes.

**The verifier did not refuse this sheet.** `verifier.py` has a foreign-cut
check (a cut entering another part); a T13 groove entering the WDC should
have been refused and wasn't. Find out why (likely grooves are exempted, or
checked at centerline instead of swept width, or not checked past their own
part's box) — that gap gets closed regardless of the generator fix.

### Failure 2 — the break

Cut order in the emitted file: T13 grooves → T17 V-slots → **T11 openings to
Z 0.15 → T12 detail through to Z −0.002** → T11 perimeter pass 1 (Z 0.06) →
pass 2 (Z −0.006). So the opening dropouts are **completely freed** (held by
vacuum only) before the perimeter is ever touched. During the perimeter cut
the frame is a thin MDF ring with a loose slab beside it; the T11 sets up
vibration and the stile snaps — exactly what the shop photos show (broken
stile on the WDC frame, cracked rail on the W3330).

---

## 2. Change 1 — clamp T13 grooves inside the part

**New rule: no T13 groove cut may extend beyond its part's bounding box.**

- Clamp each groove's endpoint centerlines to
  `[part_edge + tool_radius, part_edge − tool_radius]` on the groove's long
  axis, so the swept cut ends **flush with the part edge** (the groove still
  reaches the edge — full length through the rail ends — it just doesn't
  exit). Confirm the exact endpoint with Scott in M1: flush, or a small inset
  (the WDC rail grooves' stile-line stop is the in-codebase precedent for a
  clamped groove).
- This applies to the stile grooves (the only ones that currently overrun).
  Rail grooves already stop at the stile centerlines — leave them alone.
- Do not touch the groove Z (0.55), insets (0.5625 / 0.9375), feeds, or the
  T17 slot geometry. The WDC cone overrun (0.875 past the stile ends) is a
  **different, intentional** behavior with its own clearance rule — do not
  "fix" it while you're in there.
- With the clamp in place the nester needs **no new spacing constraint** for
  grooves (the cut can no longer reach a neighbor by construction). Do not
  add one.

**Verifier (independent authority — fix it separately from the generator):**

- Close the gap found in §1: the foreign-cut check must catch a groove whose
  **swept width** (centerline ± tool radius) enters a foreign part or exits
  its own part's box beyond the allowed envelope. It must refuse the exact
  R0805 nest as emitted by the *old* generator.
- The verifier must not simply share the generator's clamping code — it
  re-checks independently, per project ethos.

---

## 3. Change 2 — tabbed holding + T12 release section

**Goal:** nothing is fully separated — dropout from frame, or frame from
skeleton — until a final, slow, tabs-only T12 section at the very end of the
program. Scott's parameters:

- **Tab height: 0.25"** of material left standing (cut floor at **Z 0.25**;
  Z0 = spoilboard = bottom of stock, so tabs sit on the face side of the
  face-down frame — that's fine, they are milled away completely at release).
- **Tab length: 0.75"** at full height, measured along the path, **plus
  ramps** at each end using the existing `ramp_ratio` (2 horizontal : 1
  vertical — from Z −0.006 to Z 0.25 that's ≈0.51" per ramp, ≈1.77" total
  footprint per tab).
- **Spacing: target ≤ 8–10" between tabs, minimum 2 per side** (roughly 2 on
  a short side, 3–4 on a long side); a side too short to fit two gets one,
  centered. Applied to **both** the opening profiles and the part perimeters.

### 3a. Tab placement (deterministic — no randomness, per project ethos)

- Evenly distributed along each side, symmetric about the side's midpoint.
- Keep every tab (including its ramps) **≥ 2" from corners** and **clear of
  the lead-in / lead-out spans** (perimeter lead-in ramps are ~4" long —
  never overlap them; relocate the tab, don't shrink it).
- Tabs are independent of grooves and V-slots in Z (groove floor 0.55,
  V-slot floor 0.3125, both above the 0.25 tab top), so a tab may sit under
  a groove crossing — no special handling needed, but assert it in tests.

### 3b. How tabs are formed

**Every pass that cuts below Z 0.25 lifts over the tab zones** — ramp up to
Z 0.25, traverse the 0.75" at 0.25, ramp back to the pass depth:

- T11 opening pass (Z 0.15) — lifts.
- T12 detail pass (Z −0.002) — lifts over the same tab zones (same
  angular positions on the profile, so one tab block spans both kerfs).
- T11 perimeter **pass 1** (Z 0.06) — lifts (it must, or the skin pass
  destroys the tab before pass 2 can preserve it).
- T11 perimeter **pass 2** (Z −0.006) — lifts.
- T13 / T17 never cut below 0.25 — unaffected.

Keep the existing two-pass perimeter structure (onion skin + through) as-is
under the tabs. Raise in M1 whether the 0.06 skin still earns its keep once
tabs exist; do not remove it without Scott's say-so.

### 3c. The release section (new, always last)

A new final section after perimeter pass 2 — **T12 (0.2" downshear), tab
zones only, slow**:

- For each tab: approach inside the already-open kerf at `approach_z`,
  plunge slowly, feed through the tab segment (full-height span **plus both
  ramps plus a small overlap**) down to the through depth (−0.002), retract
  to `rapid_z`, move to the next tab. Only ~0.252" of material is actually
  milled (everything above Z 0.25 is already open kerf).
- **Path offset — this is the subtle part.** The T12 is narrower than the
  T11 kerf (0.2 vs 0.375). A release pass down the T11 centerline would
  leave a ~0.09" rib of tab attached to the finished part edge. Instead the
  release path must run **flush with the finished profile**: centerline
  offset from the finished edge by the T12 radius, into the waste side —
  i.e., for openings, exactly the T12 detail path (re-trace it through the
  tab zones); for perimeters, the equivalent flush offset. The tab remnant
  then rides away on the waste (dropout / skeleton) side, and the part edge
  is clean.
- **Order:** all opening tabs first, then perimeter tabs, inners-before-
  hosts — consistent with the existing pass-2 convention. The very last
  motions of the program are the release cuts; after them everything is
  free, exactly once, at minimum cutting force.
- **Feeds:** "very slowly" per Scott. Propose concrete numbers in M1 —
  starting suggestion ≈50% of the T12 detail feeds (e.g. 293 → ~150 IPM
  feed, 100 → ~50 plunge) — and get Scott's sign-off; these become named
  `PostConfig` values, not literals.

### 3d. Everything downstream of the emitter

- `CutPlan` grows a **release section**; `plan.sections` order becomes
  T13 → T17 (if WDC) → T11 openings → T12 detail → T11 perimeter pass 1 →
  pass 2 → **T12 release**.
- The verifier's **cut-order rules** must learn the new order, and gain a new
  safety rule: **no profile may be fully separated before the release
  section** — every through profile must retain its minimum tab set until
  release. Refuse a program that frees a part early.
- If the 3D simulation has landed: parts/dropouts are now **freed at
  release**, not at perimeter pass 2 — update the "freed" semantics, the
  cut-list panel, and its tests. Tabs should be visible in the progressive
  reveal (uncut bridges in the kerf).
- The cut-sheet PDF report: add a one-line note per sheet that parts are
  tab-held with a T12 release pass, so operators know the parts will not be
  loose until the very end. Keep it minimal.

---

## 4. Regression fixture — this exact sheet

Reconstruct the R0805 nest (1×W3330 rotated + 1×WDC2436, the same placement)
as a permanent test fixture. Assert:

- Old groove behavior would be **refused by the fixed verifier** (foreign
  cut into the WDC).
- New generator output: grooves clamped (no cut outside any part box, no cut
  past the sheet edge), tabs present on both parts' openings and perimeters
  at the specified size/spacing, release section last, and the verifier
  passes the sheet.

---

## 5. Byte-exactness policy for this change

This prompt **intentionally changes emitted `.anc` output**, so the
R710101N / R720101N / R730101N byte-for-byte round-trips cannot survive
as-is. Handle it deliberately, not by weakening tests:

- The reference `.anc` files remain untouched in the repo as the **measured
  source of constants** — they are documentation of the machine's dialect
  and of the pre-amendment behavior.
- Regenerate the golden outputs for those three programs with the new
  generator, produce an **annotated diff** (every changed line explained:
  clamped groove endpoint / tab lift / release section), and **get Scott's
  explicit sign-off on the diff before blessing the new goldens**. The
  round-trip tests then assert byte-exactness against the *new* goldens.
- Everything the amendment does not touch must be byte-identical in that
  diff — headers, T17 slots, rail grooves, feeds, lead-ins. If anything else
  moved, the change is wrong.
- If the simulation work refactored the emitter to a motion stream with a
  byte-exact guarantee, coordinate: the motion stream is where tabs and
  release naturally live; the guarantee re-anchors on the new goldens.

---

## 6. Testing (strictly TDD — match the existing ~766-test suite)

- **Groove clamping:** for every part type with stile grooves, assert no
  groove's swept cut leaves the part box; assert rail grooves unchanged;
  assert the WDC's groove treatment unchanged.
- **Verifier foreign-cut:** the old R0805 emission is refused with a finding
  locating the groove and the WDC; swept-width (not centerline) geometry;
  independent of generator code paths.
- **Tab placement:** counts and spacing per side length (2 on 14", 3–4 on
  30–33"), ≥2" corner clearance, no lead-in/out overlap, determinism (same
  input → same tabs), single-tab fallback on very short sides.
- **Tab formation:** every pass below Z 0.25 lifts over every tab zone with
  correct ramp geometry; passes at/above 0.25 never lift; tab block spans
  both opening kerfs (T11 + T12 detail) at the same positions.
- **Release:** section is last; opening tabs before perimeter tabs;
  inners-before-hosts; path flush with the finished profile (offset checked
  numerically); Z reaches −0.002; slow feeds from `PostConfig`; every tab
  gets exactly one release cut.
- **Hold invariant (verifier):** no profile fully separated before release;
  crafted violations are refused.
- **Goldens:** new golden round-trips byte-exact; annotated-diff review
  recorded in the repo (a `docs/` note is fine).
- Every existing test stays green except the three golden round-trips, which
  are re-blessed per §5 — nothing else changes.

---

## 7. Milestones (build in order, test each, check in after each; commit only when asked)

1. **Groove clamp + verifier gap.** Diagnose why the verifier passed R0805;
   fix the foreign-cut check; clamp stile grooves; the §4 fixture; confirm
   the exact clamped endpoint (flush vs small inset) with Scott.
2. **Tab model (headless).** Tab placement engine + the lift-over-tabs
   behavior in every deep pass; placement and formation tests. Propose the
   release feeds to Scott.
3. **Release section.** T12 tabs-only release with the flush path offset;
   plan/section changes; verifier cut-order + hold-invariant rules.
4. **Goldens + downstream.** Regenerate goldens, annotated diff, Scott's
   sign-off; simulation + report updates if applicable; `RESUME.md` update.

---

## 8. Things NOT to do

- **Do not add a nesting spacing rule for grooves** — the clamp makes it
  unnecessary by construction (Scott chose clamping over clearance).
- **Do not touch the T17 V-slot geometry or its 0.875 cone rule.**
- **Do not run the release pass as a full slow lap** of every profile —
  tab zones only (Scott's explicit choice).
- **Do not leave tab ribs on finished edges** — the flush offset in §3c is
  mandatory; a centerline release pass is wrong even though it "works".
- **Do not weaken or delete the round-trip tests** — re-bless them through
  the annotated-diff process with Scott's sign-off, nothing less.
- **Do not invent feeds, Z levels, or tool geometry** — new values (release
  feeds) are proposed to Scott and land as named `PostConfig` entries.
- **Do not reimplement safety in the generator** — the verifier stays the
  independent authority and must catch generator regressions on its own.

---

## Acceptance test

Rebuild the R0805 job (1×W3330 + 1×WDC2436 on one sheet). The emitted
program shows: clamped stile grooves that end flush at the part edges (no
cut in the 0.455" gap, nothing past the 49" sheet edge); tabs at the
specified size and spacing on both openings and both perimeters, preserved
by every deep pass; and a final T12 release section, tab zones only, slow,
flush with the finished profiles, opening tabs then perimeters,
inners-first. The fixed verifier refuses the *old* emission of this sheet
and passes the new one. The three reference programs round-trip byte-exact
against their re-blessed goldens, with an annotated diff signed off by
Scott. Cut the sheet: no divots in the neighbor, and both frames come off
the table whole.

---

## Amendments (override the body above)

**2026-08-05, Scott, M1 check-in:**

1. Groove clamp endpoint is **FLUSH** (`PanelSpec.end_inset = 0.0`) — ratified
   as implemented.
2. The reference-file grandfathering is **ratified**: R710101N and R730101N
   carry the same pre-amendment divot cut and are refused by the fixed
   verifier, findings pinned line-by-line (`LEGACY_GROOVE_FOREIGN_CUTS`);
   R720101N is clean.
3. **The 0.06 onion-skin perimeter pass is REMOVED** for generated sheets
   ("don't need it anymore" — tabs hold the parts). §3b's "keep the two-pass
   structure" is superseded. The measured two-pass dialect stays in
   `default_config` for reading/verifying the reference files.
4. Release feeds ratified: **150 IPM cut / 50 plunge** (`ReleaseSpec`).

**2026-08-05, Scott, golden sign-off:** the §5 annotated diff
(`docs/2026-08-05_golden_reblessing.md`) was approved and the six round-trip
tests re-pointed at `reference/goldens/`.

**2026-08-05, Scott, T11 max bite:** "When the 3/8 comp (T11) is being used,
only let it take a maximum of 0.4" of material per pass" — to reduce load on
the bit. Implemented as equal bites, n = ceil(depth / 0.4), generated sheets
only (`T11_MAX_BITE` in `post/from_layout.py`; `ToolSpec.max_bite`): the
perimeter runs Z0.372 then Z-0.006 (2 × 0.378), each opening runs Z0.45 then
Z0.15 (2 × 0.30). The intermediate perimeter pass reuses the measured skin
pass's 0.1895 offset (spring stock; the through pass finish-shaves it), so a
0.375 part gap is again refused by the verifier from every direction. New
`max-bite` violation kind enforces the declared ladder. Pending Scott's
eyes: the openings split (his message named the perimeter; the rule as
stated is per-tool and the opening pass took 0.60), and the equal-bites
reading of "basically cut that in half".
