from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from mediaengine.core.derivatives import render_thumbnails


def test_duplicate_thumbnail_publish_is_thread_safe(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "derivatives"
    Image.new("RGB", (96, 64), "cornflowerblue").save(source)

    def render() -> list[dict[str, object]]:
        return render_thumbnails(str(source), str(output), [64, 256])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: render(), range(12)))

    assert all(result for result in results)
    thumbnail = output / "thumb_64.webp"
    with Image.open(thumbnail) as image:
        image.verify()
