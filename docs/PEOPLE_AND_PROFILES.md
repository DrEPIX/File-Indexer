# People, profiles, and public biographies

MediaEngine supports a local, review-first workflow similar to the people view
in consumer photo libraries:

1. `acme.vision` detects faces and produces FaceNet embeddings locally.
2. The clustering endpoint groups comparable embeddings from the same model.
3. Groups containing only one face are held back by default.
4. The UI displays an unnamed group and representative region: “Who is this?”
5. A name supplied by the user confirms the current members. Models cannot
   overwrite that decision later.

Run and review clustering with:

```text
POST /api/people/cluster
GET  /api/people/suggestions
GET  /api/people/suggestions/{cluster_id}/regions
POST /api/people/suggestions/{cluster_id}/name
```

The clustering body accepts `threshold` (default `0.72`),
`min_cluster_size` (default `2`), and `limit_per_model`. Higher thresholds
produce smaller, more conservative groups. Face similarity is not proof of
identity; the review step is mandatory.

## Profile links

Profile adapters are URL builders, not scrapers. They never sign in, search for
accounts, inspect followers, or assert that an account belongs to someone.
Every link is supplied and confirmed by the user.

Built-in adapters cover Instagram, Snapchat, X/Twitter, LinkedIn, Linktree,
GitHub, Facebook, TikTok, Threads, YouTube, Bluesky, Reddit, Twitch, Pinterest,
OnlyFans, Fansly, Mastodon, ordinary websites, and custom services. Unknown and
future platforms work through a manually supplied HTTPS profile URL.

```text
GET    /api/profile-providers
POST   /api/identities/{identity_id}/profiles
DELETE /api/identities/{identity_id}/profiles/{profile_id}
```

Example:

```json
{"provider":"github","handle":"octocat","display_label":"Developer profile"}
```

Installed Python packages can add canonical URL builders with the
`mediaengine.profile_providers` entry-point group. The entry point returns a
`mediaengine.profiles.ProfileProvider` instance. Built-in IDs cannot be
overridden by third-party adapters.

## Wikipedia biography

Wikipedia access is non-generative: MediaEngine searches the MediaWiki Action
API and stores the selected page's introductory extract, URL, page ID, Wikidata
item when present, and source revision. It does not ask an LLM to rewrite the
text. Network access must be explicitly enabled with
`plugins.allow_network=true`.

```text
GET    /api/identities/{identity_id}/wikipedia/search?language=en
PUT    /api/identities/{identity_id}/wikipedia
DELETE /api/identities/{identity_id}/wikipedia?language=en
```

Search returns candidates. The caller must choose a `page_title` before the
biography is attached, preventing a same-name search result from silently
becoming a person's biography.

## Deletion and scope

`DELETE /api/biometrics` also deletes linked profiles and stored biographies,
along with face regions, embeddings, clusters, identities, reference packs,
and match suggestions. No social credentials are stored by this subsystem.
