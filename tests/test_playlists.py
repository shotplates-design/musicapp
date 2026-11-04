import pytest

from app.artists import add_song_to_artist_playlist
from app.playlists import (
    PlaylistError,
    add_track_to_manual_playlists,
    create_manual_playlist,
    delete_manual_playlist,
    remove_track_from_manual_playlist,
    rename_manual_playlist,
)


def _artist(name="Radiohead", track_ids=None):
    return {
        "id": "artist-1",
        "name": name,
        "kind": "artist",
        "artistKey": name.casefold(),
        "trackIds": list(track_ids or []),
    }


def test_create_blocks_duplicate_manual_names_case_insensitively():
    playlists = [_artist()]
    create_manual_playlist(playlists, "  Road   Trip ", playlist_id="mine")
    with pytest.raises(PlaylistError, match="already exists"):
        create_manual_playlist(playlists, "road trip")
    assert [playlist["name"] for playlist in playlists if playlist["kind"] == "manual"] == ["Road Trip"]
    assert len(playlists) == 2


def test_blank_manual_name_is_rejected():
    playlists = []
    with pytest.raises(PlaylistError, match="Name the playlist"):
        create_manual_playlist(playlists, "   ")
    assert playlists == []


def test_manual_playlist_may_reuse_an_artist_name():
    playlists = [_artist("Radiohead", ["song-1"])]
    created = create_manual_playlist(playlists, "radiohead", playlist_id="mine")
    assert created["kind"] == "manual"
    assert created["name"] == "radiohead"
    filed = add_song_to_artist_playlist(playlists, "Radiohead", "song-2")
    assert filed["id"] == "artist-1"
    assert filed["trackIds"] == ["song-1", "song-2"]
    assert playlists[1]["trackIds"] == []


def test_song_can_be_added_to_several_playlists_and_removed_from_one():
    playlists = [_artist("Pixel Band", ["song-1"])]
    create_manual_playlist(playlists, "Night", playlist_id="night")
    create_manual_playlist(playlists, "Drive", playlist_id="drive")
    added = add_track_to_manual_playlists(playlists, "song-1", ["night", "drive", "artist-1"])
    assert [playlist["id"] for playlist in added] == ["night", "drive"]
    assert playlists[1]["trackIds"] == ["song-1"]
    assert playlists[2]["trackIds"] == ["song-1"]
    assert playlists[0]["trackIds"] == ["song-1"]
    remove_track_from_manual_playlist(playlists, "night", "song-1")
    assert playlists[1]["trackIds"] == []
    assert playlists[2]["trackIds"] == ["song-1"]
    assert playlists[0]["trackIds"] == ["song-1"]


def test_rename_blocks_another_manual_name_and_allows_the_same_playlist():
    playlists = []
    create_manual_playlist(playlists, "Night", playlist_id="night")
    create_manual_playlist(playlists, "Drive", playlist_id="drive")
    with pytest.raises(PlaylistError, match="already exists"):
        rename_manual_playlist(playlists, "drive", "  NIGHT ")
    renamed = rename_manual_playlist(playlists, "drive", "  late drive ")
    assert renamed["name"] == "late drive"
    assert rename_manual_playlist(playlists, "drive", "Late Drive")["name"] == "Late Drive"


def test_rename_and_delete_leave_artist_playlists_alone():
    playlists = [_artist()]
    with pytest.raises(PlaylistError, match="not yours to rename"):
        rename_manual_playlist(playlists, "artist-1", "Mine")
    with pytest.raises(PlaylistError, match="not yours to delete"):
        delete_manual_playlist(playlists, "artist-1")
    create_manual_playlist(playlists, "Mine", playlist_id="mine")
    playlists[1]["trackIds"] = ["song-1"]
    removed = delete_manual_playlist(playlists, "mine")
    assert removed["trackIds"] == ["song-1"]
    assert [playlist["id"] for playlist in playlists] == ["artist-1"]
    assert playlists[0]["trackIds"] == []
