"""Minimal publication check for the teaching scale algorithm."""
import unittest

import plugin


class ScaleTests(unittest.TestCase):
    def test_scales_a_number(self):
        self.assertEqual(plugin.run({'value': 2}, {'factor': 3}, None), {'value': 6})
