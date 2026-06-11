from datetime import datetime, timedelta

from photoflow.dates import (
    date_from_filename,
    parse_exif_date,
    resolve_date,
    year_from_folder,
)


class TestParseExifDate:
    def test_standard_exif(self):
        assert parse_exif_date("2015:07:14 10:30:00") == datetime(2015, 7, 14, 10, 30, 0)

    def test_t_separator(self):
        assert parse_exif_date("2015:07:14T10:30:00") == datetime(2015, 7, 14, 10, 30, 0)

    def test_timezone_suffix_ignored(self):
        # wild EXIF carries tz suffixes; regex grabs the leading match
        assert parse_exif_date("2015:07:14 10:30:00+02:00") == datetime(2015, 7, 14, 10, 30)

    def test_none_input(self):
        assert parse_exif_date(None) is None

    def test_garbage(self):
        assert parse_exif_date("not a date") is None

    def test_all_zeros(self):
        assert parse_exif_date("0000:00:00 00:00:00") is None

    def test_year_below_window(self):
        assert parse_exif_date("1989:01:01 00:00:00") is None

    def test_year_above_window(self):
        future = datetime.now().year + 2
        assert parse_exif_date(f"{future}:01:01 00:00:00") is None

    def test_invalid_calendar_date(self):
        assert parse_exif_date("2015:02:30 10:00:00") is None


class TestDateFromFilename:
    def test_compact_datetime(self):
        assert date_from_filename("IMG_20190304_101112.jpg") == datetime(2019, 3, 4, 10, 11, 12)

    def test_whatsapp(self):
        assert date_from_filename("IMG-20190304-WA0001.jpg") == datetime(2019, 3, 4)

    def test_dashed_date(self):
        assert date_from_filename("2019-03-04 party.jpg") == datetime(2019, 3, 4)

    def test_no_date(self):
        assert date_from_filename("beach.jpg") is None

    def test_bogus_year_rejected(self):
        assert date_from_filename("IMG_30190304_101112.jpg") is None

    def test_invalid_compact_falls_through(self):
        # compact match with invalid date must not raise
        assert date_from_filename("99999999_999999.jpg") is None


class TestYearFromFolder:
    def test_year_in_parent(self):
        assert year_from_folder("Old Laptop/Holiday 2015/beach.jpg") == 2015

    def test_nearest_folder_wins(self):
        assert year_from_folder("2010/Trip 2015/x.jpg") == 2015

    def test_filename_year_ignored(self):
        assert year_from_folder("stuff/IMG_2015.jpg") is None

    def test_out_of_window(self):
        assert year_from_folder("Archive 1980/x.jpg") is None


def _row(exif_date=None, source_path="C:/src/x.jpg", rel_path="", mtime=None):
    return {
        "exif_date": exif_date,
        "source_path": source_path,
        "rel_path": rel_path,
        "mtime": mtime,
    }


class TestResolveDate:
    def test_exif_wins(self):
        iso, src, conf = resolve_date(
            _row(exif_date="2015:07:14 10:30:00", source_path="C:/s/IMG_20190304_101112.jpg")
        )
        assert (iso, src, conf) == ("2015-07-14T10:30:00", "exif", "high")

    def test_filename_second(self):
        iso, src, conf = resolve_date(_row(source_path="C:/s/IMG_20190304_101112.jpg"))
        assert (iso, src, conf) == ("2019-03-04T10:11:12", "filename", "medium")

    def test_folder_third(self):
        iso, src, conf = resolve_date(_row(rel_path="Holiday 2015/no_meta.png"))
        assert (iso, src, conf) == ("2015-01-01T00:00:00", "folder", "low")

    def test_mtime_last(self):
        ts = datetime(2018, 6, 1, 12, 0, 0).timestamp()
        iso, src, conf = resolve_date(_row(mtime=ts))
        assert iso.startswith("2018-06-01T12:00:00")
        assert (src, conf) == ("mtime", "low")

    def test_nothing(self):
        assert resolve_date(_row()) == (None, "none", "none")

    def test_bogus_mtime_rejected(self):
        ts = (datetime.now() + timedelta(days=365 * 3)).timestamp()
        assert resolve_date(_row(mtime=ts)) == (None, "none", "none")
