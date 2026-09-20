"""Static contract for the model-gateway navigation card."""
import unittest

import portal


class PortalNavigationTests(unittest.TestCase):
    def test_model_gateway_card_uses_the_canonical_nas_label(self):
        source = next(item for item in portal._DEFAULT_SOURCES if item["id"] == "model-gateway")
        self.assertEqual(source["name"], "模型与凭证")
        self.assertEqual(len(source["cards"]), 1)
        card = source["cards"][0]
        self.assertEqual(card["name"], "NAS 模型网关")
        self.assertEqual(card["url"], "http://100.123.208.32:39010")
        self.assertEqual(card["icon"], "🔐")
        self.assertTrue(card["ready"])


if __name__ == "__main__":
    unittest.main()
