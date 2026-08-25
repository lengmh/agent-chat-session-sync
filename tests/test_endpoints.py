import unittest

from agent_chat_session_sync.endpoints import LocalEndpoint


class LocalEndpointTests(unittest.TestCase):
    def test_network_transports_are_rejected(self) -> None:
        for value in ("tcp://127.0.0.1:9810", "ws://127.0.0.1:9810"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "local endpoint transport"):
                    LocalEndpoint.parse(value)


if __name__ == "__main__":
    unittest.main()
