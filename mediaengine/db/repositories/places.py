"""Locations, places, and the R*Tree spatial index.

SQLite has no native spatial index. The R*Tree virtual table module gives
bounding-box queries, which is enough for the two things a photo library
actually asks: "what is in this map viewport" (a bbox query directly) and
"what is within N km of here" (a bbox pre-filter, then a haversine post-filter
to trim the corners of the box). SpatiaLite would give real geodesic
predicates, but assuming it is present is not safe, so it stays optional and
unused by the core.

``asset_geo`` stores degenerate boxes — ``min_lat == max_lat`` — because every
asset is a point. R*Tree does not mind, and it keeps radius search to one
index lookup.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from ...types import LocationSource
from ...util import utcnow_iso
from .base import Repository, row_to_dict, rows_to_dicts

__all__ = ["PlaceRepository"]

# Degrees of latitude per kilometre. Longitude is scaled by cos(lat) at use.
_KM_PER_DEG_LAT = 110.574


class PlaceRepository(Repository):
    """Geo reads and writes."""

    # ── locations ───────────────────────────────────────────────────────────

    def set_location(
        self,
        asset_id: int,
        latitude: float,
        longitude: float,
        *,
        source: LocationSource | str = LocationSource.EXIF,
        altitude_m: float | None = None,
        accuracy_m: float | None = None,
        heading_deg: float | None = None,
        place_id: int | None = None,
        producer_id: int | None = None,
        recorded_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Record a location and keep the R*Tree row in sync.

        The primary key is ``(asset_id, source)``, so EXIF and user locations
        coexist. :meth:`best_location` decides which one wins for display, and
        it always prefers the user's.
        """
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            raise ValueError(f"coordinates out of range: {latitude},{longitude}")

        def _op(c: sqlite3.Connection) -> None:
            self._upsert(
                c,
                "asset_locations",
                {
                    "asset_id": asset_id,
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude_m": altitude_m,
                    "accuracy_m": accuracy_m,
                    "heading_deg": heading_deg,
                    "place_id": place_id,
                    "source": str(source),
                    "producer_id": producer_id,
                    "recorded_at": recorded_at or utcnow_iso(),
                },
                ["asset_id", "source"],
            )
            self._reindex_geo(c, asset_id)

        self._write(_op, conn, label="set_location")

    def _reindex_geo(self, c: sqlite3.Connection, asset_id: int) -> None:
        """Rewrite the R*Tree row from the current best location."""
        row = c.execute(
            "SELECT latitude, longitude FROM asset_locations WHERE asset_id = ? "
            "ORDER BY CASE source WHEN 'user' THEN 0 WHEN 'exif' THEN 1 ELSE 2 END LIMIT 1",
            (asset_id,),
        ).fetchone()
        c.execute("DELETE FROM asset_geo WHERE id = ?", (asset_id,))
        if row is not None:
            lat, lon = float(row["latitude"]), float(row["longitude"])
            c.execute(
                "INSERT INTO asset_geo(id, min_lat, max_lat, min_lon, max_lon) VALUES (?,?,?,?,?)",
                (asset_id, lat, lat, lon, lon),
            )

    def best_location(self, asset_id: int) -> dict[str, Any] | None:
        """The location to show a user. User-set beats EXIF beats derived."""
        return row_to_dict(
            self._one(
                "SELECT * FROM asset_locations WHERE asset_id = ? "
                "ORDER BY CASE source WHEN 'user' THEN 0 WHEN 'exif' THEN 1 ELSE 2 END LIMIT 1",
                (asset_id,),
            )
        )

    def locations_for_asset(self, asset_id: int) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self._query("SELECT * FROM asset_locations WHERE asset_id = ?", (asset_id,))
        )

    def delete_location(
        self,
        asset_id: int,
        source: str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        def _op(c: sqlite3.Connection) -> None:
            if source:
                c.execute(
                    "DELETE FROM asset_locations WHERE asset_id = ? AND source = ?",
                    (asset_id, source),
                )
            else:
                c.execute("DELETE FROM asset_locations WHERE asset_id = ?", (asset_id,))
            self._reindex_geo(c, asset_id)

        self._write(_op, conn, label="delete_location")

    # ── spatial queries ─────────────────────────────────────────────────────

    def in_bbox(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        *,
        limit: int = 5000,
    ) -> list[int]:
        """Asset ids inside a bounding box. One R*Tree lookup.

        Handles a box crossing the antimeridian by splitting it in two, which
        is otherwise a silent wrong-answer bug when a user pans across the
        Pacific.
        """
        if min_lon > max_lon:
            left = self.in_bbox(min_lat, min_lon, max_lat, 180.0, limit=limit)
            right = self.in_bbox(min_lat, -180.0, max_lat, max_lon, limit=limit)
            return (left + right)[:limit]
        rows = self._query(
            "SELECT id FROM asset_geo WHERE max_lat >= ? AND min_lat <= ? "
            "AND max_lon >= ? AND min_lon <= ? LIMIT ?",
            (min_lat, max_lat, min_lon, max_lon, limit),
        )
        return [int(r["id"]) for r in rows]

    def within_radius(
        self, latitude: float, longitude: float, radius_km: float, *, limit: int = 5000
    ) -> list[tuple[int, float]]:
        """Assets within ``radius_km``, as ``(asset_id, distance_km)``.

        Bounding box first — that is the only part the index can do — then
        haversine to cut the box corners off, then sort by true distance.
        """
        d_lat = radius_km / _KM_PER_DEG_LAT
        cos_lat = math.cos(math.radians(max(-89.9, min(89.9, latitude))))
        d_lon = radius_km / (_KM_PER_DEG_LAT * max(0.01, abs(cos_lat)))

        rows = self._query(
            "SELECT g.id, haversine_km(?, ?, l.latitude, l.longitude) AS dist "
            "FROM asset_geo g JOIN asset_locations l ON l.asset_id = g.id "
            "WHERE g.max_lat >= ? AND g.min_lat <= ? AND g.max_lon >= ? AND g.min_lon <= ? "
            "AND dist IS NOT NULL AND dist <= ? "
            "GROUP BY g.id ORDER BY dist LIMIT ?",
            (
                latitude, longitude,
                latitude - d_lat, latitude + d_lat,
                longitude - d_lon, longitude + d_lon,
                radius_km, limit,
            ),
        )
        return [(int(r["id"]), float(r["dist"])) for r in rows]

    def cluster_for_map(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        *,
        zoom: int = 5,
        max_points: int = 2000,
    ) -> list[dict[str, Any]]:
        """Grid-cluster points for a map viewport.

        Snaps coordinates to a zoom-dependent grid and aggregates. Cheap,
        stable under panning, and good enough for a pin layer — proper
        supercluster behaviour belongs in the frontend.
        """
        # Cell size halves per zoom level; clamped so extremes stay sane.
        precision = max(0, min(7, zoom - 1))
        factor = float(10**precision)

        rows = self._query(
            "SELECT CAST(l.latitude * ? AS INTEGER) AS gy, "
            "       CAST(l.longitude * ? AS INTEGER) AS gx, "
            "       COUNT(*) AS n, AVG(l.latitude) AS lat, AVG(l.longitude) AS lon, "
            "       MIN(l.asset_id) AS sample_asset_id "
            "FROM asset_geo g JOIN asset_locations l ON l.asset_id = g.id "
            "WHERE g.max_lat >= ? AND g.min_lat <= ? AND g.max_lon >= ? AND g.min_lon <= ? "
            "GROUP BY gy, gx ORDER BY n DESC LIMIT ?",
            (factor, factor, min_lat, max_lat, min_lon, max_lon, max_points),
        )
        return [
            {
                "latitude": float(r["lat"]),
                "longitude": float(r["lon"]),
                "count": int(r["n"]),
                "sample_asset_id": int(r["sample_asset_id"]),
            }
            for r in rows
        ]

    def bounds(self) -> dict[str, float] | None:
        """Bounding box of the whole library, for an initial map fit."""
        row = self._one(
            "SELECT MIN(min_lat) AS s, MAX(max_lat) AS n, MIN(min_lon) AS w, MAX(max_lon) AS e "
            "FROM asset_geo"
        )
        if row is None or row["s"] is None:
            return None
        return {
            "min_lat": float(row["s"]), "max_lat": float(row["n"]),
            "min_lon": float(row["w"]), "max_lon": float(row["e"]),
        }

    def located_count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM asset_geo") or 0)

    def rebuild_geo_index(self, *, conn: sqlite3.Connection | None = None) -> int:
        """Rebuild ``asset_geo`` from ``asset_locations``. Repair operation."""

        def _op(c: sqlite3.Connection) -> int:
            c.execute("DELETE FROM asset_geo")
            cur = c.execute(
                "INSERT INTO asset_geo(id, min_lat, max_lat, min_lon, max_lon) "
                "SELECT asset_id, latitude, latitude, longitude, longitude FROM ("
                "  SELECT asset_id, latitude, longitude, "
                "  ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY "
                "    CASE source WHEN 'user' THEN 0 WHEN 'exif' THEN 1 ELSE 2 END) AS rn "
                "  FROM asset_locations) WHERE rn = 1"
            )
            return cur.rowcount

        return self._write(_op, conn, label="rebuild_geo_index")

    # ── places ──────────────────────────────────────────────────────────────

    def get_or_create_place(
        self,
        name: str,
        *,
        kind: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        country: str | None = None,
        region: str | None = None,
        city: str | None = None,
        parent_id: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Named place, hierarchical via ``parent_id``.

        The core does no reverse geocoding — that needs network access and a
        data source, which makes it a plugin's job, not the core's.
        """

        def _op(c: sqlite3.Connection) -> int:
            row = c.execute(
                "SELECT id FROM places WHERE name = ? AND kind IS ? AND parent_id IS ?",
                (name, kind, parent_id),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            return self._insert(
                c,
                "places",
                {
                    "name": name, "kind": kind, "latitude": latitude, "longitude": longitude,
                    "country": country, "region": region, "city": city, "parent_id": parent_id,
                },
            )

        return self._write(_op, conn, label="get_or_create_place")

    def list_places(self, *, parent_id: int | None = None) -> list[dict[str, Any]]:
        if parent_id is None:
            rows = self._query(
                "SELECT p.*, (SELECT COUNT(*) FROM asset_locations al WHERE al.place_id = p.id) "
                "AS asset_count FROM places p ORDER BY p.name"
            )
        else:
            rows = self._query(
                "SELECT p.*, (SELECT COUNT(*) FROM asset_locations al WHERE al.place_id = p.id) "
                "AS asset_count FROM places p WHERE p.parent_id = ? ORDER BY p.name",
                (parent_id,),
            )
        return rows_to_dicts(rows)

    def place_names_for_asset(self, asset_id: int) -> str:
        """Space-joined place names, for the FTS ``place_names`` column."""
        rows = self._query(
            "SELECT DISTINCT p.name, p.city, p.region, p.country FROM asset_locations al "
            "JOIN places p ON p.id = al.place_id WHERE al.asset_id = ?",
            (asset_id,),
        )
        parts: list[str] = []
        for row in rows:
            parts.extend(str(v) for v in (row["name"], row["city"], row["region"], row["country"]) if v)
        return " ".join(dict.fromkeys(parts))

    def assets_at_place(self, place_id: int, limit: int = 1000) -> list[int]:
        rows = self._query(
            "SELECT asset_id FROM asset_locations WHERE place_id = ? LIMIT ?", (place_id, limit)
        )
        return [int(r["asset_id"]) for r in rows]

    def delete_place(self, place_id: int, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: c.execute("DELETE FROM places WHERE id = ?", (place_id,)),
            conn,
            label="delete_place",
        )
