"""Platform adapters for canonical profile URLs.

Adapters intentionally do not log in, scrape, discover, or claim ownership of
an account. They only turn a handle supplied by the user into a canonical URL.
Unknown platforms remain supported through an explicit HTTPS URL.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.metadata import entry_points
from urllib.parse import quote, urlparse

__all__ = ["ProfileProvider", "profile_providers", "resolve_profile_link"]


_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ProfileProvider:
    id: str
    name: str
    url_template: str | None
    domains: tuple[str, ...]
    handle_example: str
    category: str = "social"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_PROVIDERS = (
    ProfileProvider("instagram", "Instagram", "https://www.instagram.com/{handle}/", ("instagram.com",), "username"),
    ProfileProvider("snapchat", "Snapchat", "https://www.snapchat.com/add/{handle}", ("snapchat.com",), "username"),
    ProfileProvider("x", "X / Twitter", "https://x.com/{handle}", ("x.com", "twitter.com"), "username"),
    ProfileProvider(
        "linkedin", "LinkedIn", "https://www.linkedin.com/in/{handle}",
        ("linkedin.com",), "profile-slug", "professional",
    ),
    ProfileProvider("linktree", "Linktree", "https://linktr.ee/{handle}", ("linktr.ee",), "username", "links"),
    ProfileProvider("github", "GitHub", "https://github.com/{handle}", ("github.com",), "username", "developer"),
    ProfileProvider("facebook", "Facebook", "https://www.facebook.com/{handle}", ("facebook.com",), "username"),
    ProfileProvider("tiktok", "TikTok", "https://www.tiktok.com/@{handle}", ("tiktok.com",), "username"),
    ProfileProvider("threads", "Threads", "https://www.threads.net/@{handle}", ("threads.net",), "username"),
    ProfileProvider(
        "youtube", "YouTube", "https://www.youtube.com/@{handle}",
        ("youtube.com", "youtu.be"), "channel handle", "video",
    ),
    ProfileProvider("bluesky", "Bluesky", "https://bsky.app/profile/{handle}", ("bsky.app",), "name.bsky.social"),
    ProfileProvider(
        "reddit", "Reddit", "https://www.reddit.com/user/{handle}/",
        ("reddit.com",), "username", "community",
    ),
    ProfileProvider("twitch", "Twitch", "https://www.twitch.tv/{handle}", ("twitch.tv",), "username", "video"),
    ProfileProvider("pinterest", "Pinterest", "https://www.pinterest.com/{handle}/", ("pinterest.com",), "username"),
    ProfileProvider("onlyfans", "OnlyFans", "https://onlyfans.com/{handle}", ("onlyfans.com",), "username", "creator"),
    ProfileProvider("fansly", "Fansly", "https://fansly.com/{handle}", ("fansly.com",), "username", "creator"),
    ProfileProvider("mastodon", "Mastodon", None, (), "full profile URL"),
    ProfileProvider("website", "Website", None, (), "full website URL", "website"),
    ProfileProvider("custom", "Other platform", None, (), "full profile URL", "other"),
)


@lru_cache(maxsize=1)
def _provider_map() -> dict[str, ProfileProvider]:
    providers = {provider.id: provider for provider in _PROVIDERS}
    for entry_point in entry_points(group="mediaengine.profile_providers"):
        try:
            loaded = entry_point.load()
            candidate = loaded() if callable(loaded) else loaded
            if isinstance(candidate, ProfileProvider) and _PROVIDER_RE.fullmatch(candidate.id):
                providers.setdefault(candidate.id, candidate)
        except Exception:
            # A broken optional adapter must not break identity/profile reads.
            continue
    return providers


def profile_providers() -> list[dict[str, object]]:
    return [provider.as_dict() for provider in _provider_map().values()]


def _valid_https_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("profile_url must be an HTTPS URL without embedded credentials")
    if parsed.port not in {None, 443}:
        raise ValueError("profile_url may only use the standard HTTPS port")
    return parsed.geturl()


def resolve_profile_link(
    provider_id: str,
    *,
    handle: str | None = None,
    profile_url: str | None = None,
) -> tuple[str, str | None, str]:
    """Return normalized ``(provider, handle, url)`` from user input."""

    provider_key = provider_id.strip().lower()
    if not _PROVIDER_RE.fullmatch(provider_key):
        raise ValueError("provider must match [a-z0-9][a-z0-9._-]{0,63}")
    provider = _provider_map().get(provider_key)
    clean_handle = (handle or "").strip().lstrip("@").strip("/") or None
    if clean_handle and any(character.isspace() for character in clean_handle):
        raise ValueError("handle may not contain whitespace")
    if profile_url:
        url = _valid_https_url(profile_url)
    elif provider is not None and provider.url_template and clean_handle:
        url = provider.url_template.format(handle=quote(clean_handle, safe="._-"))
    else:
        raise ValueError("this provider requires an explicit profile_url")

    if provider is not None and provider.domains:
        hostname = (urlparse(url).hostname or "").lower()
        if not any(hostname == domain or hostname.endswith("." + domain) for domain in provider.domains):
            raise ValueError(f"profile_url is not on a recognized {provider.name} domain")
    return provider_key, clean_handle, url
