import unittest
from unittest.mock import patch

from agent_chat_session_sync.errors import PlatformAPIError
from agent_chat_session_sync.platforms.feishu import FeishuPlatform


class FeishuPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = FeishuPlatform("app-id", "app-secret", "ou-user")
        self.platform._token = "test-token"

    def test_validate_chat_treats_dissolved_chat_as_stale(self) -> None:
        with patch.object(
            self.platform,
            "_api_json",
            side_effect=PlatformAPIError(232009, "chat has already been dissolved"),
        ):
            self.assertFalse(self.platform.validate_chat("oc-dissolved"))

    def test_validate_chat_reraises_unrelated_platform_errors(self) -> None:
        error = PlatformAPIError(999999, "unrelated")
        with patch.object(self.platform, "_api_json", side_effect=error):
            with self.assertRaises(PlatformAPIError) as raised:
                self.platform.validate_chat("oc-live")
        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
