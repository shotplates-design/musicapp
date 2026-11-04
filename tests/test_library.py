from app.library import build_track_record, safe_filename, sort_tracks, title_from_filename, total_bytes


def test_record_uses_the_pixel_music_fields():
    track = build_track_record(
        id="a",
        title="  FOMDJ ",
        artist="",
        duration=12.5,
        mime_type="audio/mpeg",
        byte_size=2048,
        artwork_size=100,
        filename="fomdj.mp3",
        added_at=5,
        source="link",
    )
    assert track["title"] == "FOMDJ"
    assert track["artist"] == "Unknown artist"
    assert track["byteSize"] == 2048
    assert track["artworkSize"] == 100
    assert track["duration"] == 12.5
    assert track["addedAt"] == 5
    assert track["source"] == "link"
    assert track["duration"] != 0
    assert set(track) >= {
        "id",
        "title",
        "artist",
        "duration",
        "mimeType",
        "byteSize",
        "artworkSize",
        "filename",
        "addedAt",
        "source",
        "sourceUrl",
    }
    assert total_bytes([track, {"byteSize": 10, "artworkSize": 5}]) == 2163


def test_sorts_newest_first_then_title():
    tracks = sort_tracks([
        {"id": "old", "title": "B", "addedAt": 1},
        {"id": "a", "title": "A", "addedAt": 2},
        {"id": "c", "title": "C", "addedAt": 2},
    ])
    assert [track["id"] for track in tracks] == ["a", "c", "old"]


def test_filename_helpers_match_pixel_music():
    assert title_from_filename("My_Song.mp3") == "My Song"
    assert title_from_filename("") == "Untitled"
    assert safe_filename("A/B?.mp3", "mp3") == "AB.mp3"


def test_zero_duration_is_unknown():
    track = build_track_record(
        id="a",
        title="x",
        artist="y",
        duration=0,
        mime_type="audio/wav",
        byte_size=1,
        filename="x.wav",
        added_at=1,
    )
    assert track["duration"] is None
