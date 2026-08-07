"""Build a MediaEngine face-reference pack from operator-supplied images.

This tool does not crawl or download anything.  The input manifest must name
local images and record their source/licensing metadata; the output contains
only FaceNet embeddings and hashes, not image pixels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image


SCHEMA = "mediaengine.face-reference-pack/1"
MODEL_ID = "facenet-vggface2"


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def load_source_manifest(input_path: Path) -> dict[str, Any]:
    """Load and validate provenance before importing heavyweight ML libraries."""

    try:
        source = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read source manifest {input_path}: {exc}") from exc
    if not isinstance(source, dict):
        raise ValueError("input must be a JSON object")
    for field in (
        "name",
        "version",
        "source_url",
        "license_name",
        "rights_statement",
        "retention_policy",
    ):
        source[field] = _required_text(source.get(field), field)
    metadata = source.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    source["metadata"] = dict(metadata)
    people = source.get("people")
    if not isinstance(people, Sequence) or isinstance(people, (str, bytes)) or not people:
        raise ValueError("people must be a non-empty array")
    seen_external_ids: set[str] = set()
    for person_index, person in enumerate(people):
        if not isinstance(person, dict):
            raise ValueError(f"people[{person_index}] must be an object")
        external_id = _required_text(
            person.get("external_id"), f"people[{person_index}].external_id"
        )
        if external_id in seen_external_ids:
            raise ValueError(f"duplicate external_id {external_id!r}")
        seen_external_ids.add(external_id)
        person["external_id"] = external_id
        person["display_name"] = _required_text(
            person.get("display_name"), f"people[{person_index}].display_name"
        )
        person["source_url"] = _required_text(
            person.get("source_url"), f"people[{person_index}].source_url"
        )
        images = person.get("images")
        if not isinstance(images, Sequence) or isinstance(images, (str, bytes)) or not images:
            raise ValueError(f"people[{person_index}].images must be a non-empty array")
        for image_index, image_spec in enumerate(images):
            if not isinstance(image_spec, dict):
                raise ValueError(
                    f"people[{person_index}].images[{image_index}] must be an object"
                )
            image_spec["path"] = _required_text(
                image_spec.get("path"),
                f"people[{person_index}].images[{image_index}].path",
            )
            image_spec["source_ref"] = _required_text(
                image_spec.get("source_ref"),
                f"people[{person_index}].images[{image_index}].source_ref",
            )
    return source


def build(
    input_path: Path,
    output_path: Path,
    *,
    device_request: str = "auto",
    overwrite: bool = False,
) -> dict[str, Any]:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("output path must differ from the source manifest")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output_path}; pass --force to replace it")
    source = load_source_manifest(input_path)

    import torch
    from facenet_pytorch import InceptionResnetV1, MTCNN

    device = "cuda" if device_request in {"auto", "cuda"} and torch.cuda.is_available() else "cpu"
    detector = MTCNN(keep_all=True, post_process=True, device=device)
    encoder = InceptionResnetV1(pretrained="vggface2").eval().to(device)

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "name": source.get("name"),
        "version": source.get("version"),
        "model_id": MODEL_ID,
        "embedding_dim": 512,
        "source_url": source.get("source_url"),
        "license_name": source.get("license_name"),
        "attribution": source.get("attribution"),
        "rights_statement": source.get("rights_statement"),
        "retention_policy": source.get("retention_policy"),
        "metadata": {**dict(source.get("metadata") or {}), "builder_device": device},
        "people": [],
    }
    base = input_path.parent
    for person_index, person in enumerate(source["people"]):
        if not isinstance(person, dict) or not isinstance(person.get("images"), list):
            raise ValueError(f"people[{person_index}] must contain an images array")
        references: list[dict[str, Any]] = []
        output_person: dict[str, Any] = {
            "external_id": person.get("external_id"),
            "display_name": person.get("display_name"),
            "source_url": person.get("source_url"),
            "metadata": person.get("metadata") or {},
            "references": references,
        }
        for image_index, image_spec in enumerate(person["images"]):
            if not isinstance(image_spec, dict) or not image_spec.get("path"):
                raise ValueError(f"people[{person_index}].images[{image_index}] needs path")
            image_path = Path(str(image_spec["path"]))
            if not image_path.is_absolute():
                image_path = (base / image_path).resolve()
            raw = image_path.read_bytes()
            with Image.open(image_path) as opened:
                image = opened.convert("RGB")
                boxes, probabilities = detector.detect(image)
                if boxes is None or probabilities is None or len(boxes) != 1:
                    found = 0 if boxes is None else len(boxes)
                    raise ValueError(f"{image_path}: expected exactly one face, found {found}")
                aligned = detector.extract(image, [boxes[0]], save_path=None)
            if aligned is None:
                raise ValueError(f"{image_path}: face alignment failed")
            with torch.inference_mode():
                vector = encoder(aligned.to(device))
                vector = torch.nn.functional.normalize(vector, p=2, dim=1)[0]
            references.append(
                {
                    "embedding": vector.detach().cpu().to(torch.float32).tolist(),
                    "source_ref": str(image_spec.get("source_ref") or image_path.name),
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        result["people"].append(output_person)

    # The repository performs strict schema/provenance validation at import.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="licensed source manifest JSON")
    parser.add_argument("output", type=Path, help="output reference-pack JSON")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    args = parser.parse_args()
    payload = build(
        args.input.resolve(),
        args.output.resolve(),
        device_request=args.device,
        overwrite=args.force,
    )
    print(
        f"wrote {len(payload['people'])} people to {args.output} "
        f"using {payload['model_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
