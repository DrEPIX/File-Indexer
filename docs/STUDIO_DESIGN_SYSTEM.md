# File Indexer Studio design system

Studio is the Qt-based, thumbnail-first companion to the stable V1 desktop
surface. It uses the same backend and data while keeping its visual system and
release entry point separate.

## Principles

- Content is the interface: imagery appears before technical file type.
- Motion explains state: 130–280 ms transitions indicate hover, selection,
  loading, and background work; reduced-motion disables decorative movement.
- Friendly before forensic: human names and compact badges lead; raw metadata
  remains one click away in the inspector.
- Automatic, never mysterious: watched folders rescan in the background and
  the status dock always explains what is happening.
- Local by default: previews use local derivatives or local video playback.
- Legibility is not negotiable: a palette, an accent, or a gradient setting
  that would drop text below WCAG AA is corrected, not shipped.

## Tokens, gradients, and palettes

The source of truth is `mediaengine/studio/tokens.py`. The spacing scale is
4/8/12/16/24/32 px, base corner radii are 10/16/24 px, and motion uses
130/190/280 ms.

Every palette carries three *glow* stops alongside its flat colors. They exist
because Studio paints gradients rather than flat fills, and a flat token cannot
describe "violet in the top-left corner fading to peach at the bottom-right".
Glow stops are held within a narrow luminance band of `canvas`, so body-text
contrast is a property of the palette rather than of where a widget happens to
sit on the wash. `StudioTokens` exposes the gradients as factories —
`canvas_gradient`, `brand_gradient`, `surface_gradient`, `sidebar_gradient`,
and `bloom` — so no widget mixes its own colors.

Nine palettes share one semantic token contract: **Aurora, Graphite, Sage,
Sunset, Ocean, Blossom** (light) and **Midnight, Nebula, Carbon** (dark).
Components consume semantic values such as `brand`, `surface`, `ink_soft`, and
`danger`; they never hard-code palette colors.

Three orthogonal modifiers reshape any palette:

| Modifier | What it changes |
|---|---|
| `with_accent(hex)` | Rebuilds the brand family around a user-chosen color |
| `with_intensity(0–1)` | Scales every gradient and glow; `0` is fully flat |
| `with_density(0–1.6)` | Scales corner radii from squared to very soft |
| `without_motion()` | Zeroes every duration; state still changes, instantly |

`with_accent` honours the chosen hue and saturation but searches outward from
the chosen *lightness* for the nearest value that clears 4.5:1 both as text on
`surface` and as the backdrop for the filled-button foreground. A user picking
pastel yellow gets a slightly deeper yellow, not an unreadable interface. Tests
assert this across every palette for adversarial inputs including pure white
and pure black.

Palette, accent, intensity, density, and motion all apply **immediately**. The
Settings dialog previews changes live and reverts them if you cancel.

## Components

Studio's widgets are owner-drawn rather than stylesheet-driven. Qt stylesheet
rules cascade into child widgets, which is how one `border: 1px solid` on a
panel ends up drawing a box around every label inside it; painting also lets a
palette carry gradients, blooms, and hover glows that stylesheets cannot
express.

- `GradientCanvas`: the window backdrop — a diagonal wash plus two corner blooms.
- `AnimatedButton`: primary, secondary, ghost, chip, and danger variants. Primary
  fills with the brand→accent gradient and casts a brand-tinted glow.
- `AssetCard`: rounded media tile with cached thumbnail, friendly media badge,
  analyzer tag pills, pointer-responsive depth, and video hover playback.
- `TagStrip`: elided pill row showing analyzer output inline on a tile.
- `CheckBox`: paints its own tick, because a styled Qt indicator without a
  bundled image asset is a coloured square with nothing in it.
- `InspectorPanel`: large preview, plain-language facts, the AI summary, and
  direct file actions.
- `StatusDock`: background-work state with a breathing activity pulse.
- `Toast`, `SettingsDialog`, `FolderManagerDialog`, `AnalyzerStoreDialog`.
- `AssistantDialog`: the chat window. Every tool the model runs appears as its
  own row and every change it proposes as a card holding the exact document
  that would be written, with Apply and Discard on it — an agent is only
  trustworthy to the extent that its work is legible.

## Interaction patterns

- Search reset returns to All media + Everything and clears only view state.
- Preference reset affects Studio presentation/behavior only; library roots,
  labels, the database, and originals remain intact.
- Folder removal updates saved configuration immediately, stops watching the
  root, then cleans its index records in the background. Content duplicated in
  another root remains indexed.
- Network analyzers cannot be enabled or registered without an explicit prompt,
  and no model is downloaded without a confirmation naming the model.
- An expensive analyzer's run control defaults to the narrowest useful scope
  rather than the whole library, and shows its queue as done/failed/pending.
- The filter bar under the search box is built from live facet counts, not from
  a hardcoded list: a filter pack installed this morning becomes a row of
  clickable chips as soon as it has produced values, and a pack with no results
  yet shows nothing rather than an empty promise.
- Settings ▸ Library reports where the index actually lives and warns when that
  is a temporary directory the OS may clear, offering a one-click move.
- Dialog and card layouts use expanding content columns and minimum sizes so
  controls remain separate at supported window sizes.

The classic Tk V1 interface remains an independent migration fallback.
