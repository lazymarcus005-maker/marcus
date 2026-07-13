from harness.runtime.file_writes import atomic_write_text, content_hash


def test_atomic_write_reports_pre_and_post_image_hashes(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text("before", encoding="utf-8")

    result = atomic_write_text(path, "after")

    assert path.read_text(encoding="utf-8") == "after"
    assert result["pre_image_hash"] == content_hash("before")
    assert result["post_image_hash"] == content_hash("after")
    assert result["bytes_written"] == 5


def test_atomic_new_file_has_no_pre_image(tmp_path):
    result = atomic_write_text(tmp_path / "new.txt", "new")

    assert result["pre_image_hash"] is None
