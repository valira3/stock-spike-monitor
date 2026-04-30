"""v5.19.3 \\u2014 active dashboard tab persists across redeploys.

The active tab used to live only in a body data-attribute set in-memory,
so every fresh page load (including post-redeploy) snapped the user back
to Main. v5.19.3 routes selectTab through localStorage so the user lands
on whichever tab they last viewed.

Tested as a string-level audit of dashboard_static/app.js. A behavioral
test would need a full DOM harness; the wiring is small enough that a
text pin is sufficient and runs in pytest without Playwright.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard_static" / "app.js"


def _read_app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_app_js_exists():
    assert APP_JS.exists(), f"missing {APP_JS}"


def test_storage_key_is_namespaced():
    """Use a tg-prefixed key so it doesn't collide with future settings."""
    src = _read_app_js()
    assert 'const TG_TAB_KEY = "tg-active-tab";' in src


def test_select_tab_writes_to_localstorage():
    """selectTab(name) must persist the choice via localStorage.setItem."""
    src = _read_app_js()
    # The save helper is invoked at the top of selectTab so every code
    # path through it (click, programmatic, restore) keeps storage in
    # sync with the active state.
    assert "_tgSaveActiveTab(name);" in src
    assert "window.localStorage.setItem(TG_TAB_KEY, name)" in src


def test_load_helper_validates_against_known_tabs():
    """_tgLoadActiveTab must filter out values not in TABS to avoid a stuck dashboard."""
    src = _read_app_js()
    idx = src.find("function _tgLoadActiveTab")
    assert idx >= 0, "_tgLoadActiveTab not defined"
    block = src[idx : idx + 400]
    assert "TABS.includes(v)" in block, "load helper must validate against TABS"


def test_boot_restores_persisted_tab():
    """On boot, after click handlers wire up, restore the last tab if not Main."""
    src = _read_app_js()
    # The restore call is intentionally guarded so booting straight to
    # Main (the default) does not run an extra selectTab pass and the
    # default-attribute path stays a noop.
    assert "const __tgInitialTab = _tgLoadActiveTab();" in src
    assert "selectTab(__tgInitialTab);" in src


def test_storage_failures_are_swallowed():
    """Private browsing / disabled storage must not crash the dashboard."""
    src = _read_app_js()
    save_idx = src.find("function _tgSaveActiveTab")
    load_idx = src.find("function _tgLoadActiveTab")
    assert save_idx >= 0 and load_idx >= 0
    save_block = src[save_idx : save_idx + 200]
    load_block = src[load_idx : load_idx + 400]
    # Both helpers must wrap their localStorage call in try/catch.
    assert "try {" in save_block and "catch" in save_block
    assert "try {" in load_block and "catch" in load_block
