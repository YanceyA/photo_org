from pathlib import Path

from photoflow.naming import dest_for, slugify


class TestSlugify:
    def test_spaces_and_punct(self):
        assert slugify("My Photo (1)!") == "My-Photo-1"

    def test_truncated_to_slug_max(self):
        assert len(slugify("a" * 100)) == 40

    def test_empty_falls_back(self):
        assert slugify("???") == "img"


def _row(
    content_hash="deadbeefcafe",
    source_path="C:/src/Beach Day.jpg",
    ext=".jpg",
    date_taken=None,
    date_source="none",
):
    return {
        "content_hash": content_hash,
        "source_path": source_path,
        "ext": ext,
        "date_taken": date_taken,
        "date_source": date_source,
    }


class TestDestFor:
    OUT = Path("C:/lib")

    def test_exif_with_time(self):
        d = dest_for(_row(date_taken="2015-07-14T10:30:00", date_source="exif"), self.OUT)
        assert d == self.OUT / "2015" / "07" / "20150714_103000_Beach-Day_deadbeef.jpg"

    def test_filename_source_with_time(self):
        d = dest_for(_row(date_taken="2019-03-04T10:11:12", date_source="filename"), self.OUT)
        assert d.name == "20190304_101112_Beach-Day_deadbeef.jpg"

    def test_exif_midnight_gets_date_only_name(self):
        d = dest_for(_row(date_taken="2015-07-14T00:00:00", date_source="exif"), self.OUT)
        assert d.name == "20150714_Beach-Day_deadbeef.jpg"

    def test_low_confidence_source_gets_date_only_name(self):
        # folder/mtime sources never get an HHMMSS component
        d = dest_for(_row(date_taken="2018-06-01T12:00:00", date_source="mtime"), self.OUT)
        assert d.name == "20180601_Beach-Day_deadbeef.jpg"

    def test_dateless_goes_to_unknown(self):
        d = dest_for(_row(), self.OUT)
        assert d == self.OUT / "unknown-date" / "Beach-Day_deadbeef.jpg"

    def test_ext_lowercased(self):
        d = dest_for(_row(ext=".JPG"), self.OUT)
        assert d.suffix == ".jpg"
