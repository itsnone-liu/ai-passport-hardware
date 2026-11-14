"""Secret decoding must match the device's provision rule exactly.

The failure this prevents is invisible: both sides compute a valid HMAC, of
different messages' keys, and the only symptom is auth_failed.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from server.config import Config, decode_secret  # noqa: E402


class TestSecretDecoding(unittest.TestCase):
    HEX64 = b"32f9c62ccb67b64f349d8b184f03acf9a308b4db7682a5cd1afdda682374ee44"

    def test_64_hex_decodes_to_32_bytes(self):
        key = decode_secret(self.HEX64)
        self.assertEqual(32, len(key))
        self.assertEqual(bytes.fromhex(self.HEX64.decode()), key)

    def test_hex_with_trailing_newline_still_decodes(self):
        self.assertEqual(32, len(decode_secret(self.HEX64 + b"\n")))

    def test_uppercase_hex_decodes(self):
        self.assertEqual(bytes.fromhex(self.HEX64.decode()),
                         decode_secret(self.HEX64.upper()))

    def test_plain_text_secret_is_used_verbatim(self):
        self.assertEqual(b"devsecret", decode_secret(b"devsecret\n"))
        self.assertEqual(b"unit-test-secret-0123456789abcdef",
                         decode_secret(b"unit-test-secret-0123456789abcdef"))

    def test_odd_length_hex_is_treated_as_text(self):
        raw = b"abc" * 11          # 33 chars, non-hex length
        self.assertEqual(raw, decode_secret(raw))

    def test_short_hex_is_treated_as_text(self):
        self.assertEqual(b"abcd", decode_secret(b"abcd"))

    def test_config_loads_file_secret_as_bytes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = os.path.join(tmp, "s.secret")
            with open(secret_path, "w") as f:
                f.write(self.HEX64.decode() + "\n")
            cfg_path = os.path.join(tmp, "c.json")
            with open(cfg_path, "w") as f:
                f.write('{"devices": {"psp-000001": {"secret": "file:%s"}}}' % secret_path)
            cfg = Config.from_env(cfg_path)
            self.assertEqual(32, len(cfg.devices["psp-000001"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
