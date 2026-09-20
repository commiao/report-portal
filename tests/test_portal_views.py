"""Static contract for the portal's list / thumbnail (tile) view switching."""
import unittest

import portal


class PortalViewTests(unittest.TestCase):
    def test_both_view_modes_are_laid_out_and_switchable(self):
        html = portal._PORTAL_HTML
        # Each mode owns its own layout rules...
        self.assertIn(".v-list .grid", html)
        self.assertIn(".v-tile .grid", html)
        # ...and there is a button per mode to switch between them.
        self.assertIn("data-v=list", html)
        self.assertIn("data-v=tile", html)
        # List is the default; tiles drop the description to stay compact.
        self.assertIn("<body class=v-list>", html)
        self.assertIn(".v-tile .d{display:none}", html)

    def test_view_choice_survives_the_auto_refresh(self):
        """The page reloads itself every 120s, so the choice must be persisted —
        otherwise switching to tiles silently reverts a couple of minutes later."""
        html = portal._PORTAL_HTML
        self.assertIn("http-equiv=refresh", html)
        self.assertIn("localStorage.setItem(KEY", html)
        self.assertIn("localStorage.getItem(KEY", html)
        # Storage access throws in private mode / blocked cookies; must not break the page.
        self.assertIn("catch(e){}", html)

    def test_card_fields_are_escaped(self):
        """Card name/desc/url come from other services' manifests, so they are
        interpolated through esc() rather than straight into innerHTML."""
        html = portal._PORTAL_HTML
        self.assertIn("function esc(s)", html)
        self.assertNotIn("+r.name+", html)
        self.assertNotIn("+r.desc+", html)
        self.assertNotIn('href="\'+r.url+\'"', html)


if __name__ == "__main__":
    unittest.main()
