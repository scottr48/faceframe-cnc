# Reference files — Faceframe Optimizer project

Ground truth for the build. Where anything in the prompt and these files
disagree, THESE FILES WIN. All .anc files ran on the real machine.

## nc_files/  (real production NC programs)

| File | What it demonstrates |
|---|---|
| R710101N.anc | Baseline footprint packing: 4 wall frames (2x 30x30, 18x30, 30x12), 0.375" gaps, T13→T11→T12→T11 sequence. Preferred post style. |
| R720101N.anc | **KEY FILE — nested frames**: 18x30 placed INSIDE a 27x42's opening; 30x12 rotated to 12x30 inside a 24x42's opening. Shows the cut ordering for frame-inside-frame (all openings incl. inners' in first T11 pass; all perimeters in final T11 pass, 2 passes each). |
| R730101N.anc | **Drawer frames**: 3DB24 upright, 3DB30 rotated 90°, B30 rotated 90°, B18 upright. Validates opening geometry (5 / 9.875 / remainder; base 5 + door) and proves drawer frames rotate. Also shows T11 leaves ~0.01/side for the 0.200 T12 finish pass. |
| R620101N.anc | Older post variant using T2 (3/8 downshear) roughing with ~15 shallow Z passes per opening. Roughing section optional in the app. |
| R620102N.anc | Companion partial sheet (single 30x12) from the same job. |

## layout_screenshots/  (what the CAM showed for each NC file)

PNG per NC file, named to match. Use them to confirm your decoded part
placements agree with what the CAM displayed. R620101N_cutsheet_report.pdf
is the CAM's printable cut-sheet page format — the app's PDF report should
convey the same info (parts, sizes, labels, sheet id, barcode optional).

## orders/  (input spreadsheets)

- 7-21-26_Cab_Tec_Order_with_specs.xls — PRIMARY TEST ORDER (acceptance
  test in the prompt uses this). Known quirks: WDC2436 missing width
  (correct value 24), SD1212 has no frame dims (exclude after prompting).
- 7-7-26_Cab_Tec_Order_with_specs.xls — second sample for parser testing.

## spec_drawings/  (handwritten geometry specs)

- 3_drawer_and_base_frame_specs.JPG — the 3-drawer and base frame rules
  (1.5" members; openings 5 / 9.875 / remainder; base = 5" drawer over
  door). The routed OPENINGS follow these; the drawer FACE sizes written
  beside them are the applied fronts, NOT the openings.
- Wall_Frame.jpeg — wall frames are exact outside size, 1.5" frame all
  around, one opening.
