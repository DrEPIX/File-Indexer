# Filter packs

A filter pack is a TOML file that adds one axis to your library: which sport a
clip shows, whether it is animated, where a file came from, whether it is safe
for everyday browsing. Each pack names a namespace and a closed list of labels;
the engine turns it into an analyzer, and every label becomes a search filter
and a facet.

No Python is involved. The core has never heard of "sport" — it stores
namespaced annotations and groups them at query time, and a pack is the
user-facing way to put something in a namespace.

## What ships

| Pack | Facet | Sorts into | Method |
|---|---|---|---|
| `filters.origin` | Came from | Twitch, YouTube, TikTok, screen recording, phone camera, dedicated camera, chat app | rules |
| `filters.genre` | Genre | movies, shows, gameshows, news, sport, games, memes, art, live TV, recorded TV, adult | vision |
| `filters.format` | Kind | movie, TV episode, documentary, stream VOD, clip, trailer, music video, tutorial, home video, gameplay, screen capture, sports broadcast | vision |
| `filters.sport` | Sport | football, American football, basketball, baseball, ice hockey, tennis, combat sports, motorsport, cycling, athletics, swimming, golf, cricket, rugby, esports, extreme sports | vision |
| `filters.animation` | Animation | live action, anime, western cartoon, 3D/CGI, stop motion, motion graphics, pixel art, mixed media | vision |
| `filters.safety-nsfw` | Content rating | safe, needs review, flagged | vision |

Add them from Studio's **AI Analyzer Store ▸ Filters** (`Ctrl+M`). Adding a
filter enables its analyzer; **Sort now** runs it over the scope you pick.
**Install from file…** in the same tab adds a `.toml` pack someone else wrote,
and packs you installed carry a **Delete** button — shipped ones do not, because
switching a shipped pack off is what removing it means.

### Genre, format and sport are three questions, not one

`filters.genre` answers "what shelf does this go on". `filters.format` answers
"how was it made". They overlap in wording and not in use: a meme compilation of
gameshow clips is `content.genre:memes` and `content.format:clip`, while the
broadcast it was cut from is `content.genre:gameshows` and
`content.format:tv-episode`. Install whichever axes you actually sort by —
combining them in one query is the point:

```text
content.genre:movies content.animation:anime      # anime films
content.genre:sports -content.format:clip         # full matches, not highlights
```

`content.genre:adult` is a shelf, not a screening tool. For actually keeping
adult content out of everyday browsing, install `filters.safety-nsfw`, which
rates every asset and is tuned to send uncertain cases to review.

## The three methods

**`rules`** matches regular expressions against a file's own name, path, MIME
type, container metadata, and extracted text. It needs no model, no network,
and no pixels, and it runs at thousands of files per minute. Platform origin is
a naming convention, so that is what `filters.origin` uses.

**`vision`** samples keyframes and asks a local vision model to pick one value
from the pack's list. This is what "which sport is this" requires.

**`text`** sends metadata and extracted document text to a local model, for
axes that live in words rather than pixels.

Vision and text packs need a model selected in the Model Store. Choosing one
there points every model-backed pack at it in the same action — a pack left on
"whatever loaded first" will silently get a text-only model and fail on video.

## Confidence

Rule packs score by how much evidence matched, on a curve that saturates and
never reaches 1.0 — a regular expression over a filename is a strong hint, not
a proof. One weight-2.0 hit scores 0.75; four corroborating rules score 0.98.
That is what makes `conf:>=0.9` mean something.

Model packs record the model's own confidence and drop anything under the
pack's `threshold`. Every pack includes a way for the model to decline; a
question like "which sport is this" asked about a desktop recording otherwise
gets answered with the nearest sport rather than with silence.

## Searching through them

```text
sport:football                 # canonical
sport:footy                    # any alias the pack declares
content.sport:football         # the full namespace always works
kind:gameplay                  # the facet's display name works too
-origin.platform:youtube       # exclude
nsfw:safe                      # only media examined and rated safe
not:safety.nsfw:flagged        # exclude flagged, keep unrated
```

Aliases also widen plain text search: typing `footy` searches for `football`
and `soccer` as well. Expansion is additive, so an alias can never shrink a
result set on a library where that pack has not run yet.

## Installing one someone else wrote

Packs are plain files, so sharing one means sharing a `.toml`. **Install from
file…** validates it before copying it into your pack folder — a pack that
lands in the folder and only then turns out to be malformed looks exactly like
one that installed and did nothing, so the refusal happens at import and names
the line that is wrong.

Deleting a pack removes the file, not the tags. The annotations it produced stay
in the database with their provenance, so reinstalling the same pack picks them
straight back up rather than re-analysing the library.

## Writing your own

Click **Write your own** in the Filters tab to open your pack folder, or copy
one of the shipped files from `mediaengine/filters/builtin/`.

```toml
[pack]
id = "filters.mood"          # also the analyzer id; [a-z0-9._-]
name = "Mood"
display_name = "Mood"        # the facet heading, and a search key
version = "1.0.0"
namespace = "content.mood"   # where the annotations land
method = "vision"            # rules | vision | text
accepts = ["video", "image"]
multi_label = false          # true to allow several values at once
threshold = 0.45
description = "How a clip feels."

[[label]]
name = "calm"
display = "Calm"
aliases = ["peaceful", "chill"]
hint = "slow movement, soft light, quiet composition"
```

A `rules` pack adds patterns instead of hints:

```toml
[[label]]
name = "drone"
display = "Drone footage"
[[label.rule]]
field = "filename"           # filename | path | mime_type | text | container | encoder | title
pattern = "(?<![0-9A-Za-z])(?:dji|mavic|drone)(?![0-9A-Za-z])"
weight = 2.0
```

Use `(?<![0-9A-Za-z])…(?![0-9A-Za-z])` rather than `\b`. An underscore is a
word character, so `\btwitch\b` does not match `stream_twitch.mp4` — and
underscores are the commonest separator in media filenames.

Packs are validated on load. A file with a bad regex, a duplicate label, or an
unknown method is reported by name in the Filters tab rather than silently
skipped. A pack in your folder that reuses a shipped id replaces it.

## Editing a pack that has already run

The pack's content is hashed into its analyzer version, so changing a prompt,
a pattern, a threshold, or the vocabulary supersedes the claims the old version
made — through the ordinary version-bump path, with the audit trail intact.
Cosmetic edits (a display name, a description) do not, so renaming a label for
readability will not re-analyse your library.

## NSFW screening

`filters.safety-nsfw` labels and never hides, moves, or deletes anything;
frontends choose their own display policy. `nsfw:safe` is deliberately strict
and omits unrated media, because the backend cannot honestly call unexamined
content safe.

Image classifiers are wrong in both directions — animation, medical imagery,
art, close crops, and unusual lighting all mislead them. Treat the label as a
screening aid with a review path, and never as the sole basis for an
irreversible action. See [AI_TAGGING.md](AI_TAGGING.md) for the search syntax
and default thresholds.
