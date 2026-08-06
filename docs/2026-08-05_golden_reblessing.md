# Golden re-blessing — the 2026-08-05 amendment (job R0805)

The annotated diff spec §5 of `CLAUDE_CODE_PROMPT_Tabs_and_Groove_Clamp.md`
requires, awaiting Scott's explicit sign-off. Until that sign-off lands the
six round-trip tests keep comparing against the measured originals and keep
failing on exactly these lines; after it they are re-pointed at
`reference/goldens/` and must pass byte-for-byte.

## What the goldens are

`reference/goldens/R710101N.anc`, `R720101N.anc`, `R730101N.anc` — each
reference program reconstructed (`post/reconstruct.py`) and regenerated
(`post/generator.py`) by the amended post. The measured originals in
`reference/nc_files/` are untouched forever: they are the documentation of
the machine's dialect and of pre-amendment behaviour.

## Why tabs, the skin removal and the release section do NOT appear here

A round-trip regenerates the file's OWN plan. The reference programs were
cut with the two-pass perimeter and no tabs, so their reconstructed plans
carry the two-pass perimeter and no tabs, and the regenerated files do too.
The single-pass/tab/release behaviour is a *scheduling policy* for sheets
planned from an optimizer layout (`post/from_layout.py`); it is proven by the
R0805 fixture and the 7-21 acceptance tests, not by these goldens. The ONLY
behaviour change that lives in the emitter itself — and therefore the only
thing that may differ in this diff — is the T13 stile-groove clamp.

## The diff, line by line

Line counts are identical (324 / 324 / 456). Every changed line is a T13
stile-groove endpoint moved by exactly **0.68995** — the measured 0.375
centreline overrun taken away, plus the 0.31495 T13 radius the centreline
now stops short of the part edge, so the swept cut ends flush with the part
(0.375 + 0.31495 = 0.68995). Two line shapes appear, plus one knock-on:

*   `X.. Y.. Z2.5` — the preposition above a groove's START point;
*   `X.. F490.` / `Y.. F490.` — a groove's END point (the cut move);
*   in R710101N and R720101N only, line 107 `G0 G54 G90 ...` — the T11
    openings section head, which restates the machine's current position,
    i.e. the LAST groove's endpoint. It moves because that endpoint moved;
    it is not a new cut.

Headers, T17 slots, rail grooves, feeds, lead-ins, openings, T12 detail and
both perimeter passes are byte-identical — asserted by equal line counts
with only the lines below differing.

### R710101N — 17 lines (four plain frames)

| line | was | now |
| --- | --- | --- |
| 29 | `X29.4375 Y73.285 Z2.5` | `X29.4375 Y72.5951 Z2.5` |
| 32 | `Y60.535 F490.` | `Y61.2249 F490.` |
| 39 | `X0.5625 Y60.535 Z2.5` | `X0.5625 Y61.2249 Z2.5` |
| 42 | `Y73.285 F490.` | `Y72.5951 F490.` |
| 44 | `X-0.375 Y59.8925 Z2.5` | `X0.315 Y59.8925 Z2.5` |
| 47 | `X30.375 F490.` | `X29.6851 F490.` |
| 54 | `X-0.375 Y29.4375 Z2.5` | `X0.315 Y29.4375 Z2.5` |
| 57 | `X30.375 F490.` | `X29.6851 F490.` |
| 64 | `X30.375 Y0.5625 Z2.5` | `X29.6851 Y0.5625 Z2.5` |
| 67 | `X-0.375 F490.` | `X0.315 F490.` |
| 69 | `X31.0175 Y-0.375 Z2.5` | `X31.0175 Y0.315 Z2.5` |
| 72 | `Y30.375 F490.` | `Y29.6851 F490.` |
| 79 | `X47.8925 Y30.375 Z2.5` | `X47.8925 Y29.6851 Z2.5` |
| 82 | `Y-0.375 F490.` | `Y0.315 F490.` |
| 94 | `X30.375 Y31.0175 Z2.5` | `X29.6851 Y31.0175 Z2.5` |
| 97 | `X-0.375 F490.` | `X0.315 F490.` |
| 107 | `G0 G54 G90 X-0.375 Y31.0175` | `G0 G54 G90 X0.315 Y31.0175` |

Note the `X-0.375` entries: the old overrun ran the tool centre 0.375 PAST a
part edge sitting at X0 — i.e. toward the machine fence, with 0.69 of cut
beyond the part. The clamp pulls all of these back inside the part.

### R720101N — 17 lines (nested frames)

| line | was | now |
| --- | --- | --- |
| 24 | `X18.131 Y61.5009 Z2.5` | `X17.4411 Y61.5009 Z2.5` |
| 27 | `X5.381 F490.` | `X6.0709 F490.` |
| 34 | `X0.5625 Y54.535 Z2.5` | `X0.5625 Y55.2249 Z2.5` |
| 37 | `Y97.285 F490.` | `Y96.5951 F490.` |
| 49 | `X22.0898 Y35.6883 Z2.5` | `X22.0898 Y34.9983 Z2.5` |
| 52 | `Y4.9383 F490.` | `Y5.6282 F490.` |
| 54 | `X26.4375 Y42.375 Z2.5` | `X26.4375 Y41.685 Z2.5` |
| 57 | `Y-0.375 F490.` | `Y0.315 F490.` |
| 69 | `X5.2148 Y4.9383 Z2.5` | `X5.2148 Y5.6282 Z2.5` |
| 72 | `Y35.6883 F490.` | `Y34.9983 F490.` |
| 74 | `X0.5625 Y-0.375 Z2.5` | `X0.5625 Y0.315 Z2.5` |
| 77 | `Y42.375 F490.` | `Y41.685 F490.` |
| 79 | `X5.381 Y90.3759 Z2.5` | `X6.0709 Y90.3759 Z2.5` |
| 82 | `X18.131 F490.` | `X17.4411 F490.` |
| 94 | `X23.4375 Y97.285 Z2.5` | `X23.4375 Y96.5951 Z2.5` |
| 97 | `Y54.535 F490.` | `Y55.2249 F490.` |
| 107 | `G0 G54 G90 X23.4375 Y54.535` | `G0 G54 G90 X23.4375 Y55.2249` |

### R730101N — 16 lines (drawer frames; no section-head knock-on)

| line | was | now |
| --- | --- | --- |
| 29 | `X0.5625 Y60.535 Z2.5` | `X0.5625 Y61.2249 Z2.5` |
| 32 | `Y91.285 F490.` | `Y90.5951 F490.` |
| 34 | `X-0.375 Y59.8925 Z2.5` | `X0.315 Y59.8925 Z2.5` |
| 37 | `X30.375 F490.` | `X29.6851 F490.` |
| 44 | `X-0.375 Y29.4375 Z2.5` | `X0.315 Y29.4375 Z2.5` |
| 47 | `X30.375 F490.` | `X29.6851 F490.` |
| 54 | `X30.375 Y0.5625 Z2.5` | `X29.6851 Y0.5625 Z2.5` |
| 57 | `X-0.375 F490.` | `X0.315 F490.` |
| 59 | `X31.0175 Y-0.375 Z2.5` | `X31.0175 Y0.315 Z2.5` |
| 62 | `Y30.375 F490.` | `Y29.6851 F490.` |
| 69 | `X47.8925 Y30.375 Z2.5` | `X47.8925 Y29.6851 Z2.5` |
| 72 | `Y-0.375 F490.` | `Y0.315 F490.` |
| 84 | `X30.375 Y31.0175 Z2.5` | `X29.6851 Y31.0175 Z2.5` |
| 87 | `X-0.375 F490.` | `X0.315 F490.` |
| 89 | `X23.4375 Y91.285 Z2.5` | `X23.4375 Y90.5951 Z2.5` |
| 92 | `Y60.535 F490.` | `Y61.2249 F490.` |

## What the regenerated programs prove

The amended verifier returns ZERO findings on all three goldens (the
originals R710101N and R730101N carry five grandfathered foreign-cut
findings each — `LEGACY_GROOVE_FOREIGN_CUTS` in `tests/test_post.py` — the
same divot cut job R0805 finally made visible; the regenerated files no
longer contain it).

## Sign-off

- [x] Scott has read the diff above and approves re-pointing the six
      round-trip tests at `reference/goldens/`.

Signed off: Scott (in session, "sign off")  date: 2026-08-05

The six tests now read their expected bytes from `reference/goldens/` via
the `golden()` helpers in `tests/test_post.py` and `tests/test_motion.py`;
reconstruction still starts from the measured originals.

## Addendum, 2026-08-05 later the same day — the T11 max-bite rule

Recorded rather than folded into the signed text above, which stands as
approved.

Scott ratified one more generated-sheet policy on 2026-08-05, after the
sign-off: *"When the 3/8 comp (T11) is being used, only let it take a maximum
of 0.4 inch of material per pass. That will help reduce the load on it."*  So
a generated sheet's perimeter now runs two equal 0.378 bites (Z0.372 then the
measured through pass at Z-0.006) and each opening two equal 0.3 bites (Z0.45
then the measured Z0.15) — `T11_MAX_BITE` / `generated_post_passes` /
`generated_opening_passes` in `post/from_layout.py`.

Two things this changes about the section "Why tabs, the skin removal and the
release section do NOT appear here":

*   the sentence "the single-pass/tab/release behaviour is a scheduling
    policy" should now read "the pass-ladder/tab/release behaviour": the
    perimeter is two passes again on a generated sheet, at neither of the
    measured dialect's depths;
*   the max-bite ladder is one more such policy and is likewise absent from
    these goldens, for exactly the reason given there — a round trip
    regenerates the file's OWN plan, and the reference programs' plans carry
    the measured T11 depths.

**The three golden files are byte-identical to the ones signed off above**
(re-verified after the max-bite change: same SHA-256, same mtimes, and the six
round-trip tests still pass against them unchanged). Nothing in this addendum
needs a new sign-off; it is here so the sentence above cannot mislead a later
reader.
