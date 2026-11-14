"""资料结构的读取回归检查。"""
import json
from pathlib import Path
import tempfile
import unittest
from src.exposure_overlay_input import load_bundle, COLLECTION

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.json"

class InputTests(unittest.TestCase):
    def read_value(self, value=None, text=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(text if text is not None else json.dumps(value, ensure_ascii=False), encoding="utf-8")
            return load_bundle(path)

    def test_sample_roundtrip(self):
        expected = json.loads(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(load_bundle(SAMPLE), expected)
        self.assertTrue(expected[COLLECTION])

    def test_bad_version(self):
        data = load_bundle(SAMPLE)
        data["schema_version"] = True
        with self.assertRaises(ValueError):
            self.read_value(data)

    def test_missing_collection(self):
        data = load_bundle(SAMPLE)
        del data[COLLECTION]
        with self.assertRaises(ValueError):
            self.read_value(data)

    def test_root_shape(self):
        with self.assertRaises(ValueError):
            self.read_value([])

    def test_duplicate_key(self):
        with self.assertRaises(ValueError):
            self.read_value(text='{"schema_version": 1, "schema_version": 2}')

    def test_nonfinite(self):
        with self.assertRaises(ValueError):
            self.read_value(text='{"schema_version": 1, "value": NaN}')

if __name__ == "__main__":
    unittest.main()
