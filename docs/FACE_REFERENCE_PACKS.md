# Local face-reference packs

MediaEngine can compare locally extracted `vision.face` embeddings with an
operator-created reference pack. It does not ship a celebrity image database,
scrape photos, or download a pack. This keeps the source rights, intended use,
and retention decision with the library owner.

The workflow is deliberately review-first:

1. Assemble licensed reference images and document their source.
2. Build a pack with the same `facenet-vggface2` encoder as `acme.vision`.
3. Import the JSON pack through the API.
4. Run matching. This creates `pending` suggestions only.
5. Accept or reject each suggestion. Only an explicit accept creates a
   user-confirmed identity link.

The database stores embeddings, source hashes, URLs, attribution and policy
metadata. It never stores the reference image bytes. Rejected suggestions stay
rejected across matching runs. Deleting a pack removes its templates and
suggestions. `DELETE /api/biometrics` removes all packs alongside every local
face template, face region, cluster and identity link.

## Build a pack

Copy `plugins-available/vision-http/face-reference-source.example.json`, replace
every placeholder, and point it at local image files. Each reference image must
contain exactly one detectable face.

```powershell
python plugins-available/vision-http/build_reference_pack.py `
  my-reference-source.json my-reference-pack.json --device auto
```

The builder requires the vision service dependencies (`torch`,
`facenet-pytorch`, Pillow). It performs no network request except any initial
model-weight retrieval performed by `facenet-pytorch` when its cache is empty.

## API

```text
POST   /api/face-reference-packs
GET    /api/face-reference-packs
DELETE /api/face-reference-packs/{pack_id}
POST   /api/face-reference-packs/{pack_id}/match
GET    /api/face-match-suggestions?status=pending
POST   /api/face-match-suggestions/{suggestion_id}/accept
POST   /api/face-match-suggestions/{suggestion_id}/reject
```

Example match body:

```json
{"threshold": 0.72, "min_margin": 0.05, "limit": 100000}
```

Tune thresholds against held-out local examples. A face similarity result is a
candidate, not proof of identity: pose, age, lighting, compression and lookalike
faces can all produce errors. Keep review enabled and avoid using a match for
consequential decisions.
