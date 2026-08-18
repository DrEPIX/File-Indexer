"""External profile links and sourced biographies for named identities."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from ...errors import NotFoundError
from ...util import json_dumps, json_loads, utcnow_iso
from .base import Repository, rows_to_dicts

__all__ = ["IdentityProfileRepository"]


class IdentityProfileRepository(Repository):
    def add_profile(
        self,
        identity_id: int,
        *,
        provider: str,
        handle: str | None,
        profile_url: str,
        display_label: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> dict[str, Any]:
            if c.execute("SELECT 1 FROM identities WHERE id=?", (identity_id,)).fetchone() is None:
                raise NotFoundError(f"identity {identity_id} does not exist")
            self._upsert(
                c,
                "identity_profiles",
                {
                    "identity_id": identity_id,
                    "provider": provider,
                    "handle": handle,
                    "profile_url": profile_url,
                    "display_label": display_label,
                    "user_confirmed": 1,
                    "metadata_json": json_dumps(dict(metadata or {})),
                    "created_at": now,
                    "updated_at": now,
                },
                ["identity_id", "provider", "profile_url"],
                ["handle", "display_label", "user_confirmed", "metadata_json", "updated_at"],
            )
            row = c.execute(
                "SELECT * FROM identity_profiles WHERE identity_id=? AND provider=? AND profile_url=?",
                (identity_id, provider, profile_url),
            ).fetchone()
            assert row is not None
            return self._decode(dict(row))

        return self._write(_op, None, label="add_identity_profile")

    def profiles(self, identity_id: int) -> list[dict[str, Any]]:
        if self._one("SELECT 1 FROM identities WHERE id=?", (identity_id,)) is None:
            raise NotFoundError(f"identity {identity_id} does not exist")
        return [
            self._decode(row)
            for row in rows_to_dicts(
                self._query(
                    "SELECT * FROM identity_profiles WHERE identity_id=? "
                    "ORDER BY provider, display_label, handle",
                    (identity_id,),
                )
            )
        ]

    def delete_profile(self, identity_id: int, profile_id: int) -> bool:
        return bool(
            self._write(
                lambda c: c.execute(
                    "DELETE FROM identity_profiles WHERE id=? AND identity_id=?",
                    (profile_id, identity_id),
                ).rowcount,
                None,
                label="delete_identity_profile",
            )
        )

    def save_biography(self, identity_id: int, biography: Mapping[str, Any]) -> dict[str, Any]:
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> dict[str, Any]:
            if c.execute("SELECT 1 FROM identities WHERE id=?", (identity_id,)).fetchone() is None:
                raise NotFoundError(f"identity {identity_id} does not exist")
            values = {
                "identity_id": identity_id,
                "provider": str(biography.get("provider") or "wikipedia"),
                "language": str(biography.get("language") or "en"),
                "page_id": biography.get("page_id"),
                "page_title": str(biography["page_title"]),
                "source_url": str(biography["source_url"]),
                "summary": str(biography["summary"]),
                "description": biography.get("description"),
                "wikibase_item": biography.get("wikibase_item"),
                "source_revision": biography.get("source_revision"),
                "retrieved_at": now,
                "metadata_json": json_dumps(dict(biography.get("metadata") or {})),
            }
            self._upsert(
                c,
                "identity_biographies",
                values,
                ["identity_id", "provider", "language"],
            )
            row = c.execute(
                "SELECT * FROM identity_biographies WHERE identity_id=? AND provider=? AND language=?",
                (identity_id, values["provider"], values["language"]),
            ).fetchone()
            assert row is not None
            return self._decode(dict(row))

        return self._write(_op, None, label="save_identity_biography")

    def biographies(self, identity_id: int) -> list[dict[str, Any]]:
        return [
            self._decode(row)
            for row in rows_to_dicts(
                self._query(
                    "SELECT * FROM identity_biographies WHERE identity_id=? "
                    "ORDER BY provider, language",
                    (identity_id,),
                )
            )
        ]

    def delete_biography(self, identity_id: int, *, provider: str, language: str) -> bool:
        return bool(
            self._write(
                lambda c: c.execute(
                    "DELETE FROM identity_biographies "
                    "WHERE identity_id=? AND provider=? AND language=?",
                    (identity_id, provider, language),
                ).rowcount,
                None,
                label="delete_identity_biography",
            )
        )

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        row["metadata"] = json_loads(row.pop("metadata_json", None), {})
        return row

    def stats(self) -> dict[str, int]:
        return {
            "profile_links": int(self._scalar("SELECT COUNT(*) FROM identity_profiles") or 0),
            "biographies": int(self._scalar("SELECT COUNT(*) FROM identity_biographies") or 0),
        }
