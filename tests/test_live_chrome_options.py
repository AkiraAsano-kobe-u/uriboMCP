"""Chrome launch options (regression for #6: one leftover app copy per sync on macOS).

This checks the options the project passes. What Chrome finally receives is composed by
chromedriver and was checked by hand on macOS (see the comment in ``make_chrome_driver``).
"""

from selenium import webdriver

from uribo_mcp import live


def test_chrome_options_disable_the_macos_code_sign_clone(monkeypatch):
    seen = {}

    class FakeDriver:
        def __init__(self, service=None, options=None):
            seen["arguments"] = list(options.arguments)

        def set_page_load_timeout(self, seconds):
            pass

    monkeypatch.setattr(webdriver, "Chrome", FakeDriver)

    live.make_chrome_driver()

    switches = [a for a in seen["arguments"] if a.startswith("--disable-features=")]
    # Our own options carry one --disable-features switch: Chrome honors only the last one,
    # so a second switch would silently drop the features named in the first.
    assert len(switches) == 1
    assert "MacAppCodeSignClone" in switches[0].split("=", 1)[1].split(",")
