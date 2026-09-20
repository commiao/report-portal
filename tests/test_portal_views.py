"""Static contract for the portal's list / thumbnail (tile) view switching."""
import unittest

import portal


class PortalViewTests(unittest.TestCase):
    def test_both_view_modes_are_laid_out_and_switchable(self):
        html = portal._PORTAL_HTML
        # Each mode owns its own layout rules. The trailing brace matters:
        # without it a rename to `.v-tile .gridX{...}` still satisfies assertIn
        # while the rule no longer applies to anything (caught by mutation).
        self.assertIn(".v-list .grid{", html)
        self.assertIn(".v-tile .grid{", html)
        # ...and there is a button per mode to switch between them.
        self.assertIn("data-v=list", html)
        self.assertIn("data-v=tile", html)
        # List is the default; tiles drop the description to stay compact.
        self.assertIn("<body class=v-list>", html)
        self.assertIn(".v-tile .d{display:none}", html)

    def test_tile_mode_flattens_cards_across_sources(self):
        """Every source currently holds exactly one card. If tile mode kept the
        per-source grouping it would render one lonely tile per row, so it must
        render a single grid over all sources and demote the source to a caption."""
        html = portal._PORTAL_HTML
        self.assertIn("function renderTile()", html)
        self.assertIn("function renderList()", html)
        tile_body = html.split("function renderTile()", 1)[1].split("var KEY=", 1)[0]
        # renderTile builds ONE grid, outside the per-source loop...
        self.assertEqual(tile_body.count("<div class=grid>"), 1)
        # ...only the grouped view emits source headers...
        self.assertNotIn("class=src", tile_body)
        # ...it actually flattens rather than delegating back to the grouped
        # renderer (a `return renderList();` in front of the original body left
        # every text-based assertion above green — caught by mutation)...
        self.assertNotIn("renderList()", tile_body)
        # ...and the flattening really is a loop that collects cards.
        self.assertIn("D.forEach", tile_body)
        self.assertIn("out.push", tile_body)
        self.assertIn(".v-tile .s{display:block", html)
        self.assertIn(".v-list .s{display:none}", html)

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
