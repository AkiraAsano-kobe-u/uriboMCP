"""Chrome launch options (regression for #6: one leftover app copy per sync on macOS)."""

from selenium import webdriver

from uribo_mcp import live


def test_chrome_is_launched_without_the_macos_code_sign_clone(monkeypatch):
    seen = {}

    class FakeDriver:
        def __init__(self, service=None, options=None):
            seen["arguments"] = list(options.arguments)

        def set_page_load_timeout(self, seconds):
            seen["timeout"] = seconds

    monkeypatch.setattr(webdriver, "Chrome", FakeDriver)

    live.make_chrome_driver()

    switches = [a for a in seen["arguments"] if a.startswith("--disable-features=")]
    # Chrome honors only the last --disable-features switch, so there must be exactly one.
    assert len(switches) == 1
    assert "MacAppCodeSignClone" in switches[0].split("=", 1)[1].split(",")
