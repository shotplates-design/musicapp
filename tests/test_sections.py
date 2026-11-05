"""Cover Library / Artists / Playlists as peer sections."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_peer_section_tabs_are_in_the_shell(tmp_path):
    html = TestClient(create_app(tmp_path / "lib")).get("/").text
    assert 'data-tab="library"' in html
    assert 'data-tab="add"' in html
    assert 'data-tab="artists"' in html
    assert 'data-tab="playlists"' in html
    # Order: Library, Add, Artists, Playlists as peer nav buttons.
    lib = html.index('data-tab="library"')
    add = html.index('data-tab="add"')
    artists = html.index('data-tab="artists"')
    playlists = html.index('data-tab="playlists"')
    assert lib < add < artists < playlists


def test_client_routes_library_artists_and_playlists_separately(tmp_path):
    script = TestClient(create_app(tmp_path / "lib")).get("/js/app.js").text
    assert 'state.tab === "artists"' in script
    assert 'state.tab === "playlists"' in script
    assert "function renderArtists()" in script
    assert "function renderPlaylists()" in script
    assert "function renderLibrary()" in script
    # Library lists songs, not nested playlist sections.
    lib_start = script.index("function renderLibrary()")
    lib_end = script.index("function renderArtists()")
    library_fn = script[lib_start:lib_end]
    assert 'text: "Library"' in library_fn
    assert "No songs yet" in library_fn
    assert 'text: "Artists"' not in library_fn
    assert 'text: "Playlists"' not in library_fn
    assert "openCreatePlaylist" not in library_fn
    artists_fn = script[script.index("function renderArtists()"):script.index("function renderPlaylists()")]
    assert 'text: "Artists"' in artists_fn
    assert "!isManualPlaylist" in artists_fn
    playlists_fn = script[script.index("function renderPlaylists()"):script.index("function renderPlaylist(")]
    assert 'text: "Playlists"' in playlists_fn
    assert "openCreatePlaylist" in playlists_fn
    assert "isManualPlaylist" in playlists_fn
