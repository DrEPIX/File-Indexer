"""Content identity: hashing, magic-byte typing, perceptual hashing."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from mediaengine.core.identity import (
    Detection,
    hamming_distance,
    hash_bytes,
    hash_dirname,
    hash_file,
    detect_media_type,
    perceptual_hash,
    sniff_bytes,
)
from mediaengine.errors import CorruptMedia
from mediaengine.types import MediaType

from .conftest import write_jpeg


class TestHashing:
    def test_hash_is_prefixed_and_self_describing(self, tmp_path: Path) -> None:
        target = tmp_path / "a.bin"
        target.write_bytes(b"hello world")
        result = hash_file(target)
        assert result.content_hash.startswith(("b3:", "sha256:"))
        assert result.algorithm in ("blake3", "sha256")
        assert result.size_bytes == 11

    def test_sha256_fallback_is_selectable_and_differs(self, tmp_path: Path) -> None:
        target = tmp_path / "a.bin"
        target.write_bytes(b"hello world")
        assert hash_file(target, algorithm="sha256").content_hash.startswith("sha256:")
        # Two algorithms must never collide in the UNIQUE index, which is the
        # whole reason the digest carries its prefix.
        assert (
            hash_file(target, algorithm="sha256").content_hash
            != hash_file(target, algorithm="blake3").content_hash
        )

    def test_identical_bytes_hash_identically_from_different_paths(self, tmp_path: Path) -> None:
        (tmp_path / "one.bin").write_bytes(b"same")
        (tmp_path / "two.bin").write_bytes(b"same")
        assert (
            hash_file(tmp_path / "one.bin").content_hash
            == hash_file(tmp_path / "two.bin").content_hash
        )

    def test_zero_byte_file_hashes_without_error(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.bin"
        target.write_bytes(b"")
        result = hash_file(target)
        assert result.size_bytes == 0
        assert result.head == b""

    def test_head_is_captured_so_the_sniffer_needs_no_second_read(self, tmp_path: Path) -> None:
        target = tmp_path / "a.bin"
        target.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)
        assert hash_file(target).head.startswith(b"\xff\xd8\xff")

    def test_chunking_does_not_change_the_digest(self, tmp_path: Path) -> None:
        target = tmp_path / "big.bin"
        target.write_bytes(b"abcdefgh" * 5000)
        assert (
            hash_file(target, chunk_size=4096).content_hash
            == hash_file(target, chunk_size=1 << 20).content_hash
        )

    def test_missing_file_raises_corrupt_media(self, tmp_path: Path) -> None:
        with pytest.raises(CorruptMedia):
            hash_file(tmp_path / "nope.bin")

    def test_hash_bytes_matches_hash_file(self, tmp_path: Path) -> None:
        target = tmp_path / "a.bin"
        target.write_bytes(b"payload")
        assert hash_bytes(b"payload") == hash_file(target).content_hash


class TestDirnameSharding:
    def test_colon_is_replaced_because_windows_forbids_it(self) -> None:
        shard, name = hash_dirname("b3:9f2caa11")
        assert ":" not in name
        assert shard == "9f"
        assert name == "b3_9f2caa11"

    def test_shard_is_stable_for_the_same_hash(self) -> None:
        assert hash_dirname("sha256:abcdef") == ("ab", "sha256_abcdef")


class TestSniffing:
    @pytest.mark.parametrize(
        ("head", "expected_type", "expected_mime"),
        [
            (b"\xff\xd8\xff\xe0", MediaType.IMAGE, "image/jpeg"),
            (b"\x89PNG\r\n\x1a\n", MediaType.IMAGE, "image/png"),
            (b"GIF89a" + b"\0" * 20, MediaType.IMAGE, "image/gif"),
            (b"%PDF-1.7\n", MediaType.DOCUMENT, "application/pdf"),
            (b"fLaC" + b"\0" * 20, MediaType.AUDIO, "audio/flac"),
            (b"\x1a\x45\xdf\xa3" + b"\0" * 20, MediaType.VIDEO, "video/x-matroska"),
        ],
    )
    def test_signatures(self, head: bytes, expected_type: MediaType, expected_mime: str) -> None:
        detection = sniff_bytes(head)
        assert detection.media_type is expected_type
        assert detection.mime_type == expected_mime

    def test_ftyp_brand_separates_heic_from_mp4(self) -> None:
        # Both are ISO base media containers. Only the brand distinguishes a
        # photograph from a video, and getting it wrong sends a photo to
        # ffprobe and a video to Pillow.
        heic = sniff_bytes(b"\x00\x00\x00\x18ftypheic" + b"\0" * 20)
        mp4 = sniff_bytes(b"\x00\x00\x00\x18ftypisom" + b"\0" * 20)
        assert heic.media_type is MediaType.IMAGE
        assert mp4.media_type is MediaType.VIDEO
        assert heic.container == mp4.container == "iso-bmff"

    def test_riff_form_separates_webp_wav_and_avi(self) -> None:
        assert sniff_bytes(b"RIFF\0\0\0\0WEBP").media_type is MediaType.IMAGE
        assert sniff_bytes(b"RIFF\0\0\0\0WAVE").media_type is MediaType.AUDIO
        assert sniff_bytes(b"RIFF\0\0\0\0AVI ").media_type is MediaType.VIDEO

    def test_extension_only_disambiguates_inside_a_proven_container(self) -> None:
        tiff = b"II*\x00" + b"\0" * 20
        plain = sniff_bytes(tiff, extension=".tif")
        raw = sniff_bytes(tiff, extension=".nef")
        assert plain.mime_type == "image/tiff" and not plain.is_raw
        assert raw.is_raw and raw.mime_type == "image/x-nikon-nef"

    def test_canon_cr2_is_detected_from_bytes_not_extension(self) -> None:
        detection = sniff_bytes(b"II*\x00\x10\x00\x00\x00CR\x02\x00", extension=".dat")
        assert detection.is_raw
        assert detection.mime_type == "image/x-canon-cr2"

    def test_a_renamed_text_file_is_not_an_image(self) -> None:
        # The whole reason for magic-byte detection: an extension is a claim,
        # not evidence.
        detection = sniff_bytes(b"just some words in a file", extension=".jpg")
        assert detection.media_type is MediaType.DOCUMENT

    def test_binary_junk_is_other_not_text(self) -> None:
        assert sniff_bytes(bytes(range(256)) * 4).media_type is MediaType.OTHER

    def test_empty_head_is_other(self) -> None:
        assert sniff_bytes(b"") == Detection(MediaType.OTHER, None)

    def test_zip_members_distinguish_docx_from_epub(self, tmp_path: Path) -> None:
        docx = tmp_path / "a.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")
        epub = tmp_path / "b.epub"
        with zipfile.ZipFile(epub, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", "<container/>")

        assert "wordprocessingml" in (detect_media_type(docx).mime_type or "")
        assert detect_media_type(epub).mime_type == "application/epub+zip"

    def test_detect_reads_the_file_when_no_head_supplied(self, tmp_path: Path) -> None:
        target = write_jpeg(tmp_path / "a.jpg")
        assert detect_media_type(target).media_type is MediaType.IMAGE

    def test_detect_on_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CorruptMedia):
            detect_media_type(tmp_path / "gone.jpg")


class TestPerceptualHash:
    def test_is_sixteen_hex_characters(self, tmp_path: Path) -> None:
        target = write_jpeg(tmp_path / "a.jpg", gradient=True)
        value = perceptual_hash(target)
        assert value is not None
        assert len(value) == 16
        int(value, 16)

    def test_survives_recompression(self, tmp_path: Path) -> None:
        from PIL import Image

        original = write_jpeg(tmp_path / "a.jpg", size=(200, 200), gradient=True)
        recompressed = tmp_path / "b.jpg"
        with Image.open(original) as image:
            image.save(recompressed, quality=35)

        first, second = perceptual_hash(original), perceptual_hash(recompressed)
        assert first and second
        # The point of a perceptual hash: heavy recompression must not move it.
        assert hamming_distance(first, second) <= 4

    def test_different_images_differ(self, tmp_path: Path) -> None:
        left = write_jpeg(tmp_path / "l.jpg", size=(120, 120), gradient=True)
        right = write_jpeg(tmp_path / "r.jpg", size=(120, 120), colour=(250, 10, 10))
        assert hamming_distance(perceptual_hash(left) or "", perceptual_hash(right) or "") > 4

    def test_undecodable_file_returns_none_rather_than_raising(self, tmp_path: Path) -> None:
        # A failed phash must cost the phash, not the asset.
        target = tmp_path / "not-an-image.jpg"
        target.write_bytes(b"\xff\xd8\xff" + b"garbage" * 10)
        assert perceptual_hash(target) is None

    def test_hamming_distance_of_malformed_input_is_maximal(self) -> None:
        # Never let a corrupt hash masquerade as a near-duplicate.
        assert hamming_distance("zzzz", "0000") == 64
        assert hamming_distance("", "0000") == 64
