import os

from bot.perception.screenshot_retention import mark_captures_complete, prune_screenshots


def test_retention_skips_active_images_and_preserves_reports(tmp_path):
    screenshots = tmp_path / "screenshots"
    terrain = tmp_path / "terrain"
    completed, active = terrain / "completed", terrain / "active"
    for folder in (screenshots, completed, active):
        folder.mkdir(parents=True)
    now = 400_000.

    def write(path, age, size=1):
        path.write_bytes(bytes(size))
        os.utime(path, (now - age, now - age))
        return path

    expired = write(screenshots / "20261116_120000_camera_before_ground.png", 100_000)
    over_budget = write(screenshots / "20261120_120000_camera_before_ground.png", 60, 600_000)
    kept = write(completed / "scene.png", 10, 600_000)
    reports = [write(completed / "report.json", 120_000), write(active / "active.png", 120_000),
               write(screenshots / "manually-saved.png", 120_000)]
    mark_captures_complete(completed)
    assert prune_screenshots(24, 1, screenshots=screenshots, terrain=terrain, now=now) == (2, 600_001)
    assert not expired.exists() and not over_budget.exists()
    assert all(path.exists() for path in [kept, *reports])
