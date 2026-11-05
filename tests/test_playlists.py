import pytest

from app.playlists import (
    PlaylistError,
    add_track_to_playlists,
    create_playlist,
    delete_playlist,
    remove_track_from_playlist,
    rename_playlist,
    user_playlists,
)


def test_create_blocks_duplicate_names_case_insensitively():
    playlists = []
    create_playlist(playlists, "  Road   Trip ", playlist_id="mine")
    with pytest.raises(PlaylistError, match="already exists"):
        create_playlist(playlists, "road trip")
    assert [playlist["name"] for playlist in playlists] == ["Road Trip"]
    assert playlists[0]["kind"] == "manual"


def test_blank_name_is_rejected():
    playlists = []
    with pytest.raises(PlaylistError, match="Name the playlist"):
        create_playlist(playlists, "   ")
    assert playlists == []


def test_song_can_be_added_to_several_playlists_and_removed_from_one():
    playlists = []
    create_playlist(playlists, "Night", playlist_id="night")
    create_playlist(playlists, "Drive", playlist_id="drive")
    added = add_track_to_playlists(playlists, "song-1", ["night", "drive", "missing"])
    assert [playlist["id"] for playlist in added] == ["night", "drive"]
    assert playlists[0]["trackIds"] == ["song-1"]
    assert playlists[1]["trackIds"] == ["song-1"]
    remove_track_from_playlist(playlists, "night", "song-1")
    assert playlists[0]["trackIds"] == []
    assert playlists[1]["trackIds"] == ["song-1"]


def test_rename_blocks_another_name_and_allows_the_same_playlist():
    playlists = []
    create_playlist(playlists, "Night", playlist_id="night")
    create_playlist(playlists, "Drive", playlist_id="drive")
    with pytest.raises(PlaylistError, match="already exists"):
        rename_playlist(playlists, "drive", "  NIGHT ")
    renamed = rename_playlist(playlists, "drive", "  late drive ")
    assert renamed["name"] == "late drive"
    assert rename_playlist(playlists, "drive", "Late Drive")["name"] == "Late Drive"


def test_delete_removes_only_the_named_playlist():
    playlists = []
    create_playlist(playlists, "Mine", playlist_id="mine")
    create_playlist(playlists, "Keep", playlist_id="keep")
    playlists[0]["trackIds"] = ["song-1"]
    removed = delete_playlist(playlists, "mine")
    assert removed["trackIds"] == ["song-1"]
    assert [playlist["id"] for playlist in playlists] == ["keep"]


def test_artist_leftovers_are_ignored():
    playlists = [
        {
            "id": "artist-1",
            "name": "Radiohead",
            "kind": "artist",
            "artistKey": "radiohead",
            "trackIds": ["song-1"],
        }
    ]
    created = create_playlist(playlists, "radiohead", playlist_id="mine")
    assert created["kind"] == "manual"
    assert created["name"] == "radiohead"
    assert [playlist["id"] for playlist in user_playlists(playlists)] == ["mine"]
    with pytest.raises(PlaylistError, match="not yours to rename"):
        rename_playlist(playlists, "artist-1", "Mine")
    with pytest.raises(PlaylistError, match="not yours to delete"):
        delete_playlist(playlists, "artist-1")
