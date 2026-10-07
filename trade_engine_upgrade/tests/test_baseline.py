"""Regression contracts for behaviour intentionally retained from the original."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
import numpy as np

def load_original():
    # Numerical baseline needs no network package; never instantiate this stub.
    if importlib.util.find_spec('gspread') is None:
        sys.modules.setdefault('gspread', types.ModuleType('gspread'))
    p = Path(__file__).resolve().parents[1] / 'baseline' / 'original.py'
    spec = importlib.util.spec_from_file_location('original_baseline', p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

class BaselineContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = load_original()

    def test_long_stop(self):
        self.assertEqual(self.old.hit_logic_fast(np.array([101.]), np.array([94.]), 100., 'LONG', .05, .1)[:2], ('stop', -.05))

    def test_long_target(self):
        self.assertEqual(self.old.hit_logic_fast(np.array([111.]), np.array([99.]), 100., 'LONG', .05, .1)[:2], ('target', .1))

    def test_conservative_conflict(self):
        self.assertEqual(self.old.hit_logic_fast(np.array([111.]), np.array([94.]), 100., 'LONG', .05, .1)[0], 'same_bar_stop')

    def test_linear_round_trip_costs(self):
        self.assertAlmostEqual(self.old.apply_trade_costs(.1, 365, 2, 3, 5, 100), .089)

    def test_iid_seed(self):
        np.testing.assert_array_equal(self.old.bootstrap_iid_paths([.1, -.1], 5, 10, 42), self.old.bootstrap_iid_paths([.1, -.1], 5, 10, 42))

    def test_section_parser(self):
        out = self.old.read_section_from_values([['SECTION'], ['a','b'], ['1','2'], []], 'SECTION')
        self.assertEqual(out.iloc[0].tolist(), ['1','2'])

if __name__ == '__main__':
    unittest.main()
