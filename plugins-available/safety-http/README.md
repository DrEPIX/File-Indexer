# Local safety analyzer

This optional service classifies still images and sampled video keyframes with
a dedicated image classifier. It stores one asset-level `safety.nsfw` rating:
`safe`, `review`, or `flagged`. Video frames above the review threshold also
receive timestamped `safety.nsfw.frame` evidence.

The rating is advisory. MediaEngine does not delete, censor, move, or hide an
original automatically. A frontend may opt in to hiding `flagged` assets, and
users can inspect or override machine annotations through the normal provenance
and user-correction APIs.

Configuration under `plugins.per_plugin.local.safety`:

- `review_threshold` (default `0.45`)
- `flag_threshold` (default `0.75`)
- `max_keyframes` (default `12`, maximum `64`)

Inference is local after the model has been cached in `/models`. The first run
downloads the configured model. Set `SAFETY_MODEL_ID` to a compatible local
Hugging Face directory for an installation that never permits network access.
