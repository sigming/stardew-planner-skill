---
name: stardew-planner-skill
description: Generate and revise vanilla Stardew Valley farm, interior, coop, barn and Ginger Island layouts from reference images or ideas. Search local sprites, preserve resumable progress, review large regions independently, and deliver an editable local plan, preview and illustrated implementation diagram. Use for 星露谷物语原版布局规划、图片复刻与局部调整。
metadata:
  version: "1.0.0"
  runtime: Python 3.12 and uv
---

# Stardew layouts

Use one editable `plan.json` for every output. Interpret the reference and choose objects and positions; use scripts for catalog search, image extraction, coordinate conversion, placement checks, progress, review crops and rendering. Normal planning uses bundled assets without a browser, API key or login. The scripts do not identify objects or fully simulate game mechanics.

## Start or resume

Support only bundled vanilla farms, interiors, coops, barns and Ginger Island. Run `maps` for the current IDs and dimensions. Interiors use the bundled default room structures; spouse rooms and arbitrary farmhouse renovation combinations are unsupported. Mod maps and community maps are unsupported; do not silently replace a requested map. Establish the map, season, required facilities, preserved areas and whether the image is a precise reference or style guidance.

Set `SKILL_DIR` to this file's absolute parent directory. Keep the shell in the user's current workspace and call scripts by absolute path. Store the active plan, original reference, extracted images, checkpoints and results in a design folder under that workspace, using its absolute path as `WORK_DIR`. Preserve the original reference. Scripts use Python 3.12 and uv and declare dependencies in their headers.

For a resumed task, run `status` before editing or opening images:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" status "$WORK_DIR"
```

Read the active plan, last recorded action, latest checkpoint, pending review regions and next step. `status` compares records with the actual plan and review files; stale records require another checkpoint or check. Continue from these files rather than reconstructing progress from the conversation. Do not create a replacement plan simply because the conversation was interrupted.

For a new task:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" maps
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" new "$WORK_DIR/plan.json" --map regular
```

Editing, checkpoint, review and delivery commands automatically record results in `plan.progress.json` beside the plan. Record human judgments and the next action with `note` after a meaningful decision or before changing tasks:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" note "$WORK_DIR/plan.json" \
  --stage buildings --message '建筑类型和占地已确认；东侧道路尚未检查' --next '检查东侧道路宽度与围栏开口'
```

Use `--help` for optional arguments. Keep unresolved identities, positions, appearances and coverage decisions in the plan's `review_issues`, so delivery preserves them.

## Search and extract only needed images

Use `query_catalog.py` instead of loading the full catalog/index or guessing atlas filenames. Search by Chinese/English name, exact ID or alias. If only a category is known, use `--sheets --recognition-group GROUP`; read the returned family IDs and narrow large groups with `--recognition-family ID`.

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/query_catalog.py" --sheets --recognition-group furniture
uv run --python 3.12 "$SKILL_DIR/scripts/query_catalog.py" --sheets --recognition-group furniture --recognition-family seating
uv run --python 3.12 "$SKILL_DIR/scripts/query_catalog.py" rarecrow-1 \
  --image-out "$WORK_DIR/rarecrow-card.png" --image-kind card
uv run --python 3.12 "$SKILL_DIR/scripts/query_catalog.py" oak-tree \
  --image-out "$WORK_DIR/oak-summer.png" --image-kind sprite --season summer
```

Queries return footprint dimensions and offsets, appearance IDs, source paths, indexed card locations, `flooring_overlap` rules and building porch/mailbox metadata. Resolve ambiguous names before extracting an image. Use `--full` only when the compact record lacks needed details. When nothing matches, broaden the name or inspect the relevant category/family pages. Make a reasonable identification effort, but never force a match, invent an ID or place a guessed substitute. Keep the location as an unresolved identity issue and provide a boxed source crop at delivery.

Check roof/canopy shape, texture, upgrades, seasons and connection variants as well as color. Cabin styles and upgrade levels are distinct placements; all eight rarecrows have separate IDs. The legacy `rarecrow` is style 8. Seasonal and connected variants resolve to their base object, with the plan season and neighbors controlling rendering. Source appearance warnings remain visible in query results; an unsupported appearance requires a review issue unless covered by the simplifications below.

Mushroom Logs (`mushroom-log`, 蘑菇树桩) and Log Sections (`log-section`, 圆木段) are easy to confuse. Their cards show the other item's image and name beside their own appearances. Compare the green growth around a Mushroom Log's base with the Log Section's pale circular cut surface. `mushroom-log-ready` is the same Mushroom Log with mushrooms ready to harvest, also occupying 1 × 1 tile. Use its appearance when it is visible in the reference. Similar-item pictures are identification aids and must not be treated as variants or interchangeable objects.

Likewise, keep Lawn Flamingo ornaments (`lawn-flamingo`, 草坪火烈鸟) and Plush Bunny furniture (`plush-bunny`, 绒毛兔子), while omitting ostrich animals (`animal-ostrich`) and rabbit animals (`animal-rabbit`). Each pair has comparison pictures on both cards, explicitly labeled Keep/保留 or Omit/忽略. Do not omit the furniture merely because it resembles an animal, or place an animal using its similar furniture's ID.

Use `layout.py crop-image` for source details rather than writing a new crop script:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" crop-image "$WORK_DIR/reference.png" \
  --rect 400 240 240 180 --out "$WORK_DIR/reference-detail.png" --scale 1
```

`--rect` is `[x, y, width, height]` in source-image pixels. Atlas card bounds returned by queries are `[left, top, right, bottom]`, with right/bottom exclusive; `--image-out` handles that conversion. Neither kind of image bounds is a placement footprint.

Keep images on disk and attach only the required card, sprite or comparison. Reuse IDs, measurements and findings as text. Avoid loading a complete atlas, every seasonal source and enlarged copies for one decision. Region review uses native 16-pixel tiles regardless of output scale. `patches/index.json.image_input` reports encoded comparison-image sizes, excluding previous conversation images and API overhead. Enlarge only an unresolved detail.

Apply these layout conventions:

- Object `x/y` is the footprint's top-left in zero-based tiles. Sprite dimensions, roof/canopy overhang and transparent padding do not determine occupied tiles; use catalog footprints and offsets.
- Plan all mature trees with a 1 × 1 trunk footprint: maple, oak, pine, mahogany, desert/island palms, mushroom, mystic, all three green rain trees, and apricot, cherry, banana, mango, orange, peach, apple and pomegranate fruit trees. Their canopies may overlap other sprites. Trees may stand immediately beside buildings, including greenhouses, when the trunk is outside occupied and required construction cells. Fruit trees need the eight surrounding cells clear while growing in a 3 × 3 area; mature layouts do not reserve those cells. Ignore lightning-struck fruit-tree appearances.
- Use a 7 × 4 `slime-hutch`. Stardew Valley 1.6 reduced its footprint from 11 × 6 to 7 × 4. If the reference still shows 11 × 6, keep the current 7 × 4 asset and record the version difference as an accepted appearance issue with this convention as the resolution. Explain the difference at delivery and leave any choice of how to use the remaining space to the user.
- Fish ponds can change appearance: the orientation or arrangement of nets and other small decorations can differ, and pond water can have different colors depending on the fish raised. Recognize these as the same `fish-pond`. Always plan and render its default appearance with a 5 × 5 footprint; ignore these orientation, water-color and decoration differences. Keep one default fish-pond card in the recognition atlas, with a note about these variations. These differences do not require a separate variant, an unresolved appearance issue or user confirmation.
- Cookout Kits (`cookout-kit`, 1 × 1) and deployed Tent Kits (`tent-kit`, 3 × 2) are temporary equipment and disappear after use or overnight. Tent Kits require an outdoor map. Query results retain these placement notes; do not describe them as permanent facilities.
- Use repaired `greenhouse-repaired` for greenhouses and reserve only its 7 × 6 footprint. The centered 3 × 2 projection at relative `(2,6)` is built-in brick decoration: flooring can cover it, and kegs, furniture and other objects may occupy those tiles under their ordinary placement rules. Do not reserve that projection as an additional clearance area or draw a 7×6 + 3×2 / 7×8 footprint. The full recognition example includes the built-in bricks and a separate example with player-placed flooring. Do not identify the greenhouse's default brick effect as independent floor tiles, add flooring objects for it or include it in counts. Retain actual player-placed flooring and objects when visible. Preview rendering draws the built-in bricks beneath independent placements; the implementation diagram shows only the 7 × 6 footprint. Query `building_placement.front_decoration` and `placement_size_label` for these rules. Farmhouse upgrades retain their separate construction checks and dashed outlines; this greenhouse convention does not remove those restrictions.
- Retain kegs, floor-standing furniture, lamps and other supported equipment on building porches, even when their footprints overlap the building. Query `building_placement.porch_object_cells`: these are usable porch tiles relative to the building footprint. The validator permits overlap only there and renders the building before its contents; walls, restricted stairs, bundled mailbox cells and a second object on the same tile remain restricted. Buildings and porch objects retain separate IDs and counts. Roof overhang alone does not establish a usable porch.
- Farmhouses (`house`, `house-2`, `house-3`) and all cabin styles/upgrades have a bundled mailbox. Ignore its independent recognition, placement and count; preserve mailboxes already drawn in the building sprite or map background. The farmhouse cards include a separately labeled mailbox reference, and cabin cards label the mailbox already visible beside the cabin. `building_placement.bundled_mailbox` describes it; `mailbox`/信箱/邮箱 queries provide an omit-only reference. Do not classify it as a shipping bin, lamp or unidentified object, or add a duplicate mailbox. Its physical position is still part of the building's placement restrictions.
- Shipping Bins (`shipping-bin`, 出货箱, 2 × 1) appear in the tools/storage atlas beside the distinct Mini-Shipping Bin (`mini-shipping-bin`, 1 × 1). Opening and closing the lid does not change identity. The card includes an open-lid reference; use the default closed sprite for placement. Keep shipping bins distinct from bundled mailboxes.
- All seeds and ordinary crops use `strawberry-seeds` with a 1 × 1 footprint; all giant crops use `giant-pumpkin` with a 3 × 3 footprint. These are visual placeholders, including when the user names a variety. Query only these two crop cards. Keep decorative plants distinct.
- Omit placeable grass, blue grass and weeds; imports and recipes record them in `omitted_vegetation`. Retain terrain grass in the background and explain the simplification when it affects resemblance.
- Follow [Objects on flooring](#objects-on-flooring) for examples and queries. A tapper or heavy tapper may share the trunk tile of an oak, maple, pine, mahogany or mystic tree. Other overlaps require catalog support.
- Ignore icons, items and writing displayed on signs, including wood, stone, dark and text signs; use their default appearances. Omit objects resting on tables, chairs or similar furniture. If the underlying furniture style cannot be identified, retain an unresolved identity issue with a source box; do not select a similar or default style without the user's decision. Retain objects placed directly on flooring or supported building porches. Omit confidently identified small Torches (`torch`, 火把, sometimes described as small candles) on the ground, fences or other supports, including their flame effects. For a confirmed fence-mounted torch/candle, omit only the small light and retain the fence. Keep Candle Lamps (`candle-lamp`), Wood Lamp-posts (`wood-lamp-post`, 木灯柱), Iron Lamp-posts (`iron-lamp-post`, 铁灯柱) and all standalone braziers, including Wooden Braziers (`wooden-brazier`, 木头火炬). Generic words such as 火炬, 蜡烛 or candle do not establish the `torch` identity. For retained braziers, cauldrons and similar objects, ignore flames, smoke, glow and other active effects and use the default sprite. Apply these omissions only after identifying the underlying object.
- Distinguish a small torch/candle attached to a fence post from a standalone lamp-post or brazier beside or behind a fence. Compare the complete unlit object shape, stem or base, ground-contact tile, fence rails and neighboring fence connections using the relevant catalog sprites and footprint offsets. A flame above a fence is insufficient evidence of attachment. In particular, a Wooden Brazier vertically aligned with a wooden fence may occupy a separate tile to its north; the overlapping sprites can resemble a torch on the fence. Check their bases and north–south tile positions before applying the fence-light omission. Retain both the brazier and fence when they are separate objects. If attachment or identity remains uncertain, keep the confirmed fence and record an unresolved identity/position issue with a boxed source crop for the final summary; do not discard a possible standalone light based on vertical alignment alone.
- Ignore identifiable moving players, NPCs (including spouses and children), pets, horses, farm animals, wildlife, summoned fairy/frog companions, active Junimos and slimes. Also omit pigs' truffles and Ginger Island parrots with their perches; fixed scenery already in the map background remains. Query `--sheets --recognition-group ignored_entities` for the single combined sheet of toddlers (`toddler`, 幼儿/小孩), cats, dogs, turtles, horse, chickens, ducks, dinosaurs, rabbits, cows, ostriches, goats, sheep, pigs, butterflies, crows, owl silhouettes, seagulls, Fairy Box fairies, Frog Egg frogs, slimes, parrots/perches and truffles. All illustrated colors are recognition examples; other colors are omitted too. Fairies and frogs each use one representative card labeled as having multiple colors with the same shape; do not enumerate their colors or prismatic states. Reference thumbnails use a consistent display size, which does not represent gameplay dimensions. Do not place these references or replace them with furniture. Preserve static statues, toys, paintings, scarecrows and furniture, including animal-shaped items. If the object is still unidentified after comparison, retain an identity issue. Imports, normalization and recipes record omitted reference entities in `omitted_entities`.
- Infer obscured paths or fences only from visible endpoints, connections, spacing and repeated structure. Preserve plausible openings and record the inferred cells and evidence. When several arrangements remain plausible, retain an uncertainty instead of completing an unsupported pattern.
- When reconstructing an enclosed pen containing a barn or coop, actively look for a gate: these pens commonly include one, and a closed gate can resemble a continuous fence. Use this as a search cue, not evidence of a particular gate or location. Inspect visible openings, gate posts and fence connections, and compare the relevant gate sprites. Place and render a gate when its identity and position can be established. If they remain unclear, preserve the confirmed fence segments and openings, leave the gate unplaced and continue the reconstruction without inventing a location. Record the affected pen and coordinates in an unresolved `position` issue with empty `object_ids`; if a specific visible object has an uncertain identity, use an `identity` issue with its source box instead. Mark the relevant completed review check `uncertain`. Gate uncertainty alone does not block delivery or require a decision before the confirmed layout is delivered.
- Omit players and other humanoid characters visible in the reference. The Solid Gold Lewis statue (`solid-gold-lewis`, 纯金刘易斯像) must be retained as a placeable statue; compare its catalog sprite before treating a gold-colored human figure as a player. Human shape alone is insufficient reason to omit this statue.

### Objects on flooring

Retain supported objects and the confirmed floor/path tiles beneath them as separate placements and counts. Common examples include:

- Production and storage: `keg` (小桶/酒桶), `preserves-jar`, `furnace`, `chest`.
- Farm facilities: `sprinkler`, `scarecrow`, `bee-house`, `garden-pot`.
- Decorations: floor-standing furniture, lamps, braziers, statues, fences, gates and signs.

Query an item by ID or name:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/query_catalog.py" keg
```

Check `flooring_overlap.can_place_on_flooring`; `flooring_overlap.note_zh` and `flooring_overlap.source_urls` provide conditions and sources. Other placement restrictions and the explicit omissions above still apply.

## Calibrate, edit and save

For image reconstruction, establish the map grid before placing objects. Use at least three noncollinear fixed terrain points spanning the visible area, with extra distant points for verification. Exclude borders and captions. Preserve `fixed_landmarks`; movable houses, greenhouses, paths and trees cannot establish the grid.

Save measured pairs in `reference-points.json`, using the same point for `tile` and `pixel`, for example `[{"tile":[10,8],"pixel":[310,90]}, ...]`. Tile centers may use half coordinates. Use actual measurements, not the example values.

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" calibrate "$WORK_DIR/reference.png" \
  --map regular --points "$WORK_DIR/reference-points.json" --out "$WORK_DIR/calibration.json"
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" locate "$WORK_DIR/calibration.json" \
  --pixel 500 240 --type oak-tree --point-kind footprint
```

Calibration records image hashes and rejects residuals above 0.25 tile. This checks consistency, not landmark identity. `locate` returns the estimated position, nearest tile and rounding error without placing anything. Its default accepts the footprint top-left. Use `--point-kind sprite` only for the entire sprite canvas including transparent padding; `planner-anchor` uses the source data's bottom-left anchor. Skip calibration for text-only designs.

Reserve crop regions and coverage space early. A useful placement order is buildings, flooring/fences, facilities, trees, ornamental plants, decorations, artworks, furniture and crop placeholders. Several small categories can share a checkpoint. Apply explicit operations with stable object IDs:

```json
{"operations":[
  {"op":"add","item":{"id":"storage","type":"chest","x":20,"y":24}},
  {"op":"update","id":"storage","set":{"x":21}},
  {"op":"fill","type":"cobblestone","rect":[19,25,5,1]}
]}
```

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" apply "$WORK_DIR/plan.json" "$WORK_DIR/edits.json"
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" checkpoint "$WORK_DIR/plan.json" \
  --stage buildings --out-dir "$WORK_DIR/stages/buildings-r1" \
  --reference "$WORK_DIR/reference.png" --calibration "$WORK_DIR/calibration.json"
```

`apply` supports `add`, `update`, `remove` and `fill`; operations validate together so coordinated moves are possible. Invalid changes preserve the source; in-place edits keep one `.bak`. `set` changes title or season. `fill.rect` is `[x,y,width,height]`. `generate` with a recipe is available for rough text drafts; use explicit edits for precise reconstruction.

A checkpoint saves the actual complete plan, preview, implementation image, validation and pending visual review. With a calibrated reference it also saves an aligned overall comparison. Omit reference options for text-only work. Open a stage image when it resolves a placement or alignment question, record findings, and save the next action with `note`; do not open every generated image. Use a new checkpoint directory for each revision.

## Review regions and the complete layout

Create a final checkpoint after the layout is assembled:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" checkpoint "$WORK_DIR/plan.json" \
  --stage final --out-dir "$WORK_DIR/stages/final-r1" \
  --reference "$WORK_DIR/reference.png" --calibration "$WORK_DIR/calibration.json"
```

Image reconstruction requires both reference options. Every region gets one comparison PNG containing aligned reference, preview and implementation panels; text-only designs use preview and implementation against the requirements. The default grid is 3 columns × 2 rows. On the 80 × 65 standard farm, `--block-size 40 33` produces four regions. Each core belongs to one region with a two-tile context border for shared paths, fences, roofs and canopies. Use targeted detail crops only where needed.

When independent subagents with fresh context are supported, assign 1–2 regions per agent. Give each agent the skill path, checkpoint path, region IDs, requirements and relevant uncertainties as text. Do not inherit the main conversation's image attachments. Each agent opens only its assigned comparisons and essential details or catalog cards, checks the core and shared borders, and writes its own `patches/reviews/REGION_ID.json`. Return findings and file paths as text. Separate files prevent concurrent edits to review records.

For each region, inspect `identities`, `positions`, `flooring`, `occlusions` and `ignored_entities`. After inspection, save all five values with `layout.py review-region FINAL_DIR REGION_ID --checks CHECK=STATUS ... --finding TEXT`; repeat `--finding` for additional findings. Choose `passed`, `needs_change`, `uncertain` or `not_applicable` for each actual check; `not_applicable` requires no subject. The command validates and preserves the region and revision hashes, then saves only that region's record. Record coordinates, candidate IDs and evidence for errors or uncertainties. The index provides nearby objects, source bounds and horizontal/vertical flooring runs; inspect path widths at both ends, bends and intersections. An area outside the reference requires a textual requirement or an uncertain finding.

Agents may prepare operations or test changes on separate plan copies. The main agent applies accepted operations to the active plan sequentially and resolves conflicts. Agents must not edit the active plan concurrently. Merge completed independent records with:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" merge-review "$WORK_DIR/stages/final-r1"
```

If fresh independent contexts are unavailable, inspect regions sequentially using the same `review-region` and `merge-review` commands. `status` detects completed individual records even before they are merged and lists only the remaining regions. Use `note` for decisions and resume with `status`. Never mark a region passed solely because a previous revision passed.

Correct known errors, create a new final checkpoint, and review its regions and shared borders. The main agent then inspects the complete comparison or preview and implementation image, checking global consistency and user requirements. Save the result with `layout.py review-overall FINAL_DIR --result passed|needs_change|uncertain --finding TEXT`. The command records the inspection as completed while retaining its result and findings. A completed inspection may still contain problems.

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" check-review "$WORK_DIR/stages/final-r1"
```

This command verifies region and overall records plus plan, reference, image and relevant asset hashes. It checks record completeness and consistency; agents perform the visual inspection. Pending checks and known errors block delivery. Fully inspected uncertainty can accompany a draft, with the affected region and candidates explained to the user.

Use plan `review_issues` to retain unresolved questions:

```json
{"review_issues":[{
  "id":"east-building", "kind":"identity", "status":"unresolved",
  "description":"东侧建筑的具体类型尚未确认。",
  "object_ids":[], "candidates":["deluxe-coop","big-shed"],
  "reference_box_px":[940,150,1040,260]
}]}
```

`kind` accepts `identity`, `position`, `appearance` or `coverage`; `status` accepts `unresolved`, `resolved` or `accepted`. Resolved/accepted issues need a `resolution` describing verification or the user's decision. For a confirmed source-appearance warning, also preserve its `sprite_id`, `image_sha256` and affected `object_ids`. Leave `object_ids` empty when nothing has been identified or placed; `candidates` may also be empty. Every unidentified source object needs `reference_box_px` in original-image pixels `[left, top, right, bottom]`, right/bottom exclusive. Do not mark guesses resolved. User clarification requires editing the active plan and repeating affected visual and placement checks.

Delivery automatically crops unresolved identity boxes with context and draws a red outline using the final checkpoint's original reference. For a targeted crop during review, use the existing helper; `--rect` uses `[x,y,width,height]`, while `--box` uses the same original-image bounds as `reference_box_px`:

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" crop-image "$WORK_DIR/reference.png" \
  --rect 916 126 148 158 --box 940 150 1040 260 --out "$WORK_DIR/unidentified-detail.png"
```

## Deliver local files

```bash
uv run --python 3.12 "$SKILL_DIR/scripts/layout.py" deliver "$WORK_DIR/plan.json" \
  --review-dir "$WORK_DIR/stages/final-r1" --out-dir "$WORK_DIR/result"
```

Delivery checks placement and internal sprinkler, scarecrow, bee house and Junimo hut coverage, then saves:

- `plan.json`: editable local layout.
- `preview.png`: map terrain and seasonal sprites with coordinate ticks on all four edges, matching the implementation diagram (zero-based tile coordinates; labels every five tiles).
- `implementation.png`: footprints, matching numbers, coordinate ticks and an illustrated legend grouped by category, family and furniture series. Crop positions remain pale and unnumbered.
- `working/`: placement and coverage reports, legend data, review findings, file hashes and `recognition/unidentified-NNN.png` for unresolved identity boxes.

Do not generate a separate range diagram or material list. The default output is `./stardew-layout/`; destinations must stay in the current workspace. Use a new result revision, or `--force` only for a prior owned result folder without user-added files; it preserves an uncompressed backup. `--crop` affects the preview only. Rendering scale is 1–4; `--font` can supply a CJK font. Inspect the generated images and provide clickable output paths.

Use `working/review.json.completion_summary` in the final user-facing task summary. For each coverage gap, report the facility type, uncovered crop tile count and coordinates/regions, including sprinkler, scarecrow and applicable Junimo coverage. Preserve the reconstructed layout and let the user decide whether to keep it, tend crops manually or adjust facilities/crops; a gap alone does not block delivery or authorize redesign. Never claim full coverage while gaps remain. Sprinklers and scarecrows target ordinary crop placeholders. Junimo coverage applies when a hut exists or targets are requested. Bee houses can produce honey without flowers; placeholder crops cannot verify flower-honey access. Giant crops are excluded from ordinary automatic targets. Empty explicit targets remain unresolved. Geometric checks do not fully simulate seasons, beach watering or harvest conditions; preserve unverified requested mechanics.

For each unresolved identity, attach its cropped, boxed PNG from `completion_summary.unidentified_objects` and identify the corresponding issue/location so the user can name it. Explain that the object remains unplaced; do not describe a candidate as confirmed. If a crop record reports a missing reference or box, supply the original reference via `deliver --reference` or add the measured box, then regenerate. Fully inspected uncertainty can accompany the completed reconstruction with these omissions stated explicitly; do not wait for identity decisions before delivering the confirmed portion.

Check `working/review.json.recognition_issues` for unresolved gate positions as well, since a position issue may have no unidentified-object crop. In the final delivery summary, identify each affected barn/coop pen and explicitly leave gate placement to the user, for example: “东侧鸡舍围栏的大门位置未能确认，当前未补画，请自行设计大门位置。” Do not claim the pen has no gate merely because recognition failed, or silently add a gate to make the pen accessible. A localized ambiguous gate can also use the boxed crop workflow above.

For a user-accepted coverage gap, record an accepted `coverage` issue with `group`, `resolution` and the `coverage_fingerprint` from `working/review.json`. The gap remains visible. The decision applies only while the corresponding objects, targets, map and season remain unchanged. Do not fabricate acceptance or silently change a reviewed design to remove a gap.

## Existing inputs and maintenance

`import SOURCE --out PLAN` converts an existing local planner JSON into `plan.json`; subsequent edits use the local plan. `status` and `note` preserve ongoing work independently of the input format.

Normal planning does not refresh assets. Source URLs, hashes and crop coordinates remain in the bundled data records. Asset maintenance, validation and packaging tools live outside the installed skill in the source repository; see its README for development commands. Use the existing search, image, placement, review and delivery helpers instead of adding duplicate one-off scripts.
