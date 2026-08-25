import unittest

from agent_chat_session_sync.endpoints import (
    LocalEndpoint,
    windows_default_local_endpoint,
)


class LocalEndpointTests(unittest.TestCase):
    def test_network_transports_are_rejected(self) -> None:
        for value in ("tcp://127.0.0.1:9810", "ws://127.0.0.1:9810"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "local endpoint transport"):
                    LocalEndpoint.parse(value)

    def test_windows_default_endpoint_uses_sid_hash(self) -> None:
        endpoint = windows_default_local_endpoint("S-1-5-21-1000")

        self.assertEqual(
            str(endpoint),
            "npipe://./pipe/cc-connect-api-f051b5cbf3c10c7c",
        )


if __name__ == "__main__":
    unittest.main()
