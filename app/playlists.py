"""User-named playlists shown in the Lists tab.

Names are trimmed. A new or renamed playlist is rejected when another playlist
already uses that name, compared without case. Artist auto-playlists are not
created; any leftover artist playlists from an older build are ignored.
"""

from __future__ import annotations

import uuid

MANUAL_KIND = "manual"


class PlaylistError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def clean_name(name: str | None) -> str:
    return " ".join(str(name or "").replace("\x00", " ").split())


def name_key(name: str | None) -> str:
    return clean_name(name).casefold()


def is_user_playlist(playlist: dict) -> bool:
    """True for playlists the user created. Artist leftovers are skipped."""
    return playlist.get("kind") != "artist"


def user_playlists(playlists: list[dict]) -> list[dict]:
    return [playlist for playlist in playlists if playlist.get("kind") != "artist"]


def _playlist(playlists: list[dict], playlist_id: str) -> dict | None:
    for playlist in user_playlists(playlists):
        if playlist.get("id") == playlist_id:
            return playlist
    return None


def name_taken(playlists: list[dict], name: str, ignore_id: str | None = None) -> bool:
    key = name_key(name)
    if not key:
        return False
    for playlist in user_playlists(playlists):
        if ignore_id is not None and playlist.get("id") == ignore_id:
            continue
        if name_key(playlist.get("name") or "") == key:
            return True
    return False


def create_playlist(playlists: list[dict], name: str, playlist_id: str | None = None) -> dict:
    cleaned = clean_name(name)
    if not cleaned:
        raise PlaylistError("Name the playlist.")
    if name_taken(playlists, cleaned):
        raise PlaylistError("That playlist already exists.")
    created = {
        "id": playlist_id or str(uuid.uuid4()),
        "name": cleaned,
        "kind": MANUAL_KIND,
        "trackIds": [],
    }
    playlists.append(created)
    return created


def rename_playlist(playlists: list[dict], playlist_id: str, name: str) -> dict:
    cleaned = clean_name(name)
    if not cleaned:
        raise PlaylistError("Name the playlist.")
    playlist = _playlist(playlists, playlist_id)
    if playlist is None:
        raise PlaylistError("That playlist is not yours to rename.")
    if name_taken(playlists, cleaned, ignore_id=playlist_id):
        raise PlaylistError("That playlist already exists.")
    playlist["name"] = cleaned
    playlist["kind"] = MANUAL_KIND
    return playlist


def delete_playlist(playlists: list[dict], playlist_id: str) -> dict:
    for index, playlist in enumerate(playlists):
        if playlist.get("kind") == "artist":
            continue
        if playlist.get("id") == playlist_id:
            return playlists.pop(index)
    raise PlaylistError("That playlist is not yours to delete.")


def add_track_to_playlists(playlists: list[dict], track_id: str, playlist_ids: list[str]) -> list[dict]:
    """Put one song on each selected playlist. Other playlists stay as they are."""
    wanted = {str(item) for item in playlist_ids}
    chosen = []
    for playlist in user_playlists(playlists):
        if playlist.get("id") not in wanted:
            continue
        ids = [str(item) for item in (playlist.get("trackIds") or [])]
        if str(track_id) not in ids:
            ids.append(str(track_id))
        playlist["trackIds"] = ids
        playlist["kind"] = MANUAL_KIND
        chosen.append(playlist)
    return chosen


def remove_track_from_playlist(playlists: list[dict], playlist_id: str, track_id: str) -> dict:
    playlist = _playlist(playlists, playlist_id)
    if playlist is None:
        raise PlaylistError("That playlist is not yours to edit.")
    playlist["trackIds"] = [
        str(item) for item in (playlist.get("trackIds") or []) if str(item) != str(track_id)
    ]
    return playlist
