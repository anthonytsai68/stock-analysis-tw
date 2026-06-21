# -*- coding: utf-8 -*-
"""Unit tests for structured `.env` line preservation in ConfigManager."""

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config_manager import ConfigManager


class ConfigManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        os.environ["ENV_FILE"] = str(self.env_path)
        self.manager = ConfigManager(env_path=self.env_path)

    def tearDown(self) -> None:
        os.environ.pop("ENV_FILE", None)
        self.temp_dir.cleanup()

    def test_apply_updates_preserves_comments_blank_lines_and_raw_lines(self) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "# Core settings",
                    "STOCK_LIST=600519,000001",
                    "",
                    "export SHOULD_STAY_UNCHANGED",
                    "# Secrets",
                    "GEMINI_API_KEY=secret-key",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.manager.apply_updates(
            updates=[("STOCK_LIST", "600519,300750")],
            sensitive_keys=set(),
            mask_token="******",
        )

        env_content = self.env_path.read_text(encoding="utf-8")
        self.assertIn("# Core settings\n", env_content)
        self.assertIn("\n\nexport SHOULD_STAY_UNCHANGED\n", env_content)
        self.assertIn("# Secrets\nGEMINI_API_KEY=secret-key\n", env_content)
        self.assertIn("STOCK_LIST=600519,300750\n", env_content)

    def test_apply_updates_only_rewrites_last_duplicate_assignment(self) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "# Keep the legacy duplicate for audit history",
                    "STOCK_LIST=000001",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.manager.apply_updates(
            updates=[("STOCK_LIST", "300750")],
            sensitive_keys=set(),
            mask_token="******",
        )

        env_lines = self.env_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(env_lines[0], "STOCK_LIST=600519")
        self.assertEqual(env_lines[1], "# Keep the legacy duplicate for audit history")
        self.assertEqual(env_lines[2], "STOCK_LIST=300750")

    def test_apply_updates_falls_back_to_in_place_rewrite(self) -> None:
        self.env_path.write_text("STOCK_LIST=600519\n", encoding="utf-8")

        with patch("src.core.config_manager.os.replace", side_effect=OSError(errno.EXDEV, "cross-device")):
            self.manager.apply_updates(
                updates=[("STOCK_LIST", "000001")],
                sensitive_keys=set(),
                mask_token="******",
            )

        self.assertEqual(self.env_path.read_text(encoding="utf-8"), "STOCK_LIST=000001\n")

    def test_custom_webhook_template_placeholders_are_escaped_for_compose(self) -> None:
        template = '{"title":$title_json,"content":$content_json,"raw":$content,"name":"$OTHER"}'

        self.manager.apply_updates(
            updates=[("CUSTOM_WEBHOOK_BODY_TEMPLATE", template)],
            sensitive_keys=set(),
            mask_token="******",
        )

        env_content = self.env_path.read_text(encoding="utf-8")
        self.assertIn(
            'CUSTOM_WEBHOOK_BODY_TEMPLATE={"title":$$title_json,"content":$$content_json,'
            '"raw":$$content,"name":"$OTHER"}',
            env_content,
        )
        self.assertEqual(
            self.manager.read_config_map()["CUSTOM_WEBHOOK_BODY_TEMPLATE"],
            template,
        )

    def test_custom_webhook_template_canonicalizes_unescaped_existing_value(self) -> None:
        self.env_path.write_text(
            'CUSTOM_WEBHOOK_BODY_TEMPLATE={"content":$content_json}\n',
            encoding="utf-8",
        )

        self.manager.apply_updates(
            updates=[("CUSTOM_WEBHOOK_BODY_TEMPLATE", '{"content":$content_json}')],
            sensitive_keys=set(),
            mask_token="******",
        )

        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            'CUSTOM_WEBHOOK_BODY_TEMPLATE={"content":$$content_json}\n',
        )

    def test_custom_webhook_template_does_not_double_escape_existing_value(self) -> None:
        self.env_path.write_text(
            'CUSTOM_WEBHOOK_BODY_TEMPLATE={"content":$$content_json}\n',
            encoding="utf-8",
        )

        self.manager.apply_updates(
            updates=[("CUSTOM_WEBHOOK_BODY_TEMPLATE", '{"content":$content_json}')],
            sensitive_keys=set(),
            mask_token="******",
        )

        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            'CUSTOM_WEBHOOK_BODY_TEMPLATE={"content":$$content_json}\n',
        )
        self.assertEqual(
            self.manager.read_config_map()["CUSTOM_WEBHOOK_BODY_TEMPLATE"],
            '{"content":$content_json}',
        )

    def test_custom_webhook_template_plain_json_is_not_changed(self) -> None:
        template = '{"content":"plain json string"}'

        self.manager.apply_updates(
            updates=[("CUSTOM_WEBHOOK_BODY_TEMPLATE", template)],
            sensitive_keys=set(),
            mask_token="******",
        )

        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            f"CUSTOM_WEBHOOK_BODY_TEMPLATE={template}\n",
        )


if __name__ == "__main__":
    unittest.main()
