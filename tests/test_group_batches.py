"""群聊批次配置路由单元测试。"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

# 把插件目录加入路径
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

from core.session_config import ConfigMixin  # noqa: E402
from astrbot_plugin_proactive_chat.main import ProactiveChatPlugin  # noqa: E402


class MockSessionParser:
    """模拟 SessionMixin 的解析方法。"""

    @staticmethod
    def _parse_session_id(session_id: str):
        parts = session_id.split(":")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        return None

    @staticmethod
    def _normalize_session_id(session_id: str):
        return session_id

    @staticmethod
    def _get_session_log_str(session_id: str, config=None):
        return session_id


class ConfigTestMixin(ConfigMixin, MockSessionParser):
    def __init__(self, config: dict):
        self.config = config
        self.session_override_manager = None


class TestGroupBatchConfig(unittest.TestCase):
    def _make_config(self, group_batches=None, group_session_list=None):
        config = {
            "friend_settings": {
                "enable": True,
                "session_list": ["default:FriendMessage:123"],
                "schedule_settings": {
                    "min_interval_minutes": 30,
                    "max_interval_minutes": 600,
                    "quiet_hours": "1-7",
                    "max_unanswered_times": 4,
                },
            },
            "group_settings": {
                "enable": True,
                "session_list": group_session_list or [],
                "group_idle_trigger_minutes": 30,
                "proactive_prompt": "global_group_prompt",
                "schedule_settings": {
                    "min_interval_minutes": 90,
                    "max_interval_minutes": 360,
                    "quiet_hours": "2-6",
                    "max_unanswered_times": 2,
                },
                "auto_trigger_settings": {
                    "enable_auto_trigger": False,
                    "auto_trigger_after_minutes": 5,
                },
            },
            "group_batches": group_batches or [],
        }
        return ConfigTestMixin(config)

    def test_global_group_session(self):
        """未加入批次的群聊使用全局配置。"""
        mixin = self._make_config(group_session_list=["default:GroupMessage:100"])
        cfg = mixin._get_typed_session_config(
            "default:GroupMessage:100", "100", "group_settings", "group"
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["schedule_settings"]["min_interval_minutes"], 90)
        self.assertEqual(cfg["proactive_prompt"], "global_group_prompt")
        self.assertNotIn("_from_batch", cfg)

    def test_batch_group_session(self):
        """加入批次的群聊使用批次配置。"""
        batches = [
            {
                "batch_name": "活跃群",
                "session_list": ["default:GroupMessage:200"],
                "group_idle_trigger_minutes": 15,
                "min_interval_minutes": 60,
                "max_interval_minutes": 120,
                "quiet_hours": "3-5",
                "max_unanswered_times": 3,
                "proactive_prompt": "batch_prompt",
            }
        ]
        mixin = self._make_config(group_batches=batches)
        cfg = mixin._get_typed_session_config(
            "default:GroupMessage:200", "200", "group_settings", "group"
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["_from_batch"], "活跃群")
        self.assertEqual(cfg["group_idle_trigger_minutes"], 15)
        self.assertEqual(cfg["schedule_settings"]["min_interval_minutes"], 60)
        self.assertEqual(cfg["schedule_settings"]["max_interval_minutes"], 120)
        self.assertEqual(cfg["schedule_settings"]["quiet_hours"], "3-5")
        self.assertEqual(cfg["schedule_settings"]["max_unanswered_times"], 3)
        self.assertEqual(cfg["proactive_prompt"], "batch_prompt")

    def test_batch_inherits_unspecified_fields(self):
        """批次未指定的字段继承全局配置。"""
        batches = [
            {
                "batch_name": "部分覆盖",
                "session_list": ["default:GroupMessage:300"],
                "min_interval_minutes": 45,
            }
        ]
        mixin = self._make_config(group_batches=batches)
        cfg = mixin._get_typed_session_config(
            "default:GroupMessage:300", "300", "group_settings", "group"
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["schedule_settings"]["min_interval_minutes"], 45)
        # 未覆盖的字段保持全局值
        self.assertEqual(cfg["schedule_settings"]["max_interval_minutes"], 360)
        self.assertEqual(cfg["schedule_settings"]["quiet_hours"], "2-6")
        self.assertEqual(cfg["group_idle_trigger_minutes"], 30)
        self.assertEqual(cfg["proactive_prompt"], "global_group_prompt")

    def test_batch_priority_over_global_list(self):
        """同时命中全局 session_list 和批次时，优先使用批次。"""
        batches = [
            {
                "batch_name": "优先批次",
                "session_list": ["default:GroupMessage:400"],
                "min_interval_minutes": 10,
            }
        ]
        mixin = self._make_config(
            group_batches=batches, group_session_list=["default:GroupMessage:400"]
        )
        cfg = mixin._get_typed_session_config(
            "default:GroupMessage:400", "400", "group_settings", "group"
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["_from_batch"], "优先批次")
        self.assertEqual(cfg["schedule_settings"]["min_interval_minutes"], 10)

    def test_batch_empty_prompt_inherits_global(self):
        """批次提示词留空时继承全局。"""
        batches = [
            {
                "batch_name": "继承提示词",
                "session_list": ["default:GroupMessage:500"],
                "proactive_prompt": "",
                "min_interval_minutes": 50,
            }
        ]
        mixin = self._make_config(group_batches=batches)
        cfg = mixin._get_typed_session_config(
            "default:GroupMessage:500", "500", "group_settings", "group"
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["proactive_prompt"], "global_group_prompt")

    def test_batch_missing_fields_inherit_non_default_global_values(self):
        """归一化不能把批次缺省字段误当成显式默认值覆盖全局配置。"""
        config = self._make_config(
            group_batches=[
                {
                    "batch_name": "只改沉默时间",
                    "session_list": ["default:GroupMessage:550"],
                    "group_idle_trigger_minutes": 15,
                }
            ]
        ).config
        config["group_settings"]["schedule_settings"].update(
            {
                "min_interval_minutes": 17,
                "max_interval_minutes": 777,
                "quiet_hours": "4-9",
                "max_unanswered_times": 8,
            }
        )
        mixin = ConfigTestMixin(config)

        cfg = mixin._get_typed_session_config(
            "default:GroupMessage:550", "550", "group_settings", "group"
        )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["group_idle_trigger_minutes"], 15)
        self.assertEqual(cfg["schedule_settings"]["min_interval_minutes"], 17)
        self.assertEqual(cfg["schedule_settings"]["max_interval_minutes"], 777)
        self.assertEqual(cfg["schedule_settings"]["quiet_hours"], "4-9")
        self.assertEqual(cfg["schedule_settings"]["max_unanswered_times"], 8)

    def test_group_disabled_no_batch(self):
        """全局群聊关闭时，批次也不生效。"""
        config = self._make_config().config
        config["group_settings"]["enable"] = False
        mixin = ConfigTestMixin(config)
        cfg = mixin._get_typed_session_config(
            "default:GroupMessage:200", "200", "group_settings", "group"
        )
        self.assertIsNone(cfg)

    def test_friend_not_affected(self):
        """私聊不受批次影响。"""
        batches = [
            {
                "batch_name": "活跃群",
                "session_list": ["default:FriendMessage:123"],
                "min_interval_minutes": 10,
            }
        ]
        mixin = self._make_config(group_batches=batches)
        cfg = mixin._get_typed_session_config(
            "default:FriendMessage:123", "123", "friend_settings", "friend"
        )
        self.assertIsNotNone(cfg)
        self.assertNotIn("_from_batch", cfg)
        self.assertEqual(cfg["schedule_settings"]["min_interval_minutes"], 30)

    def test_schema_json_valid(self):
        """_conf_schema.json 中 group_batches 定义合法。"""
        schema_path = PLUGIN_DIR / "_conf_schema.json"
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)

        self.assertIn("group_batches", schema)
        self.assertEqual(schema["group_batches"]["type"], "list")
        items = schema["group_batches"]["items"]
        self.assertIn("batch_name", items)
        self.assertIn("session_list", items)
        self.assertIn("min_interval_minutes", items)
        self.assertIn("max_interval_minutes", items)
        self.assertIn("quiet_hours", items)
        self.assertIn("max_unanswered_times", items)
        self.assertIn("proactive_prompt", items)

    def test_malformed_batch_entries_are_ignored(self):
        """面板误存字符串时不能阻断其它正常批次。"""
        batches = [
            "错误的字符串批次",
            {
                "batch_name": "正常批次",
                "session_list": ["default:GroupMessage:600"],
            },
        ]
        mixin = self._make_config(group_batches=batches)

        effective = mixin._get_typed_session_config(
            "default:GroupMessage:600", "600", "group_settings", "group"
        )

        self.assertIsNotNone(effective)
        self.assertEqual(effective["_from_batch"], "正常批次")
        asyncio.run(mixin._validate_config())

    def test_null_batch_fields_do_not_block_other_batches(self):
        """旧配置中 null 批次字段不能阻断正常批次的匹配。"""
        batches = [
            {"batch_name": "坏批次", "session_list": None},
            {
                "batch_name": "正常批次",
                "session_list": ["default:GroupMessage:601"],
                "min_interval_minutes": 45,
            },
        ]
        mixin = self._make_config(group_batches=batches)

        effective = mixin._get_typed_session_config(
            "default:GroupMessage:601", "601", "group_settings", "group"
        )

        self.assertIsNotNone(effective)
        self.assertEqual(effective["_from_batch"], "正常批次")
        self.assertEqual(effective["schedule_settings"]["min_interval_minutes"], 45)

    def test_new_astrbot_upgrades_batch_schema_in_memory(self):
        """新版 AstrBot 只在内存中启用模板列表，不改写兼容旧版的静态 Schema。"""

        class SchemaConfig(dict):
            schema = {
                "group_batches": {
                    "type": "list",
                    "items": {"batch_name": {"type": "string"}},
                }
            }

        config = SchemaConfig(group_batches=[{"batch_name": "测试批次"}])
        plugin = object.__new__(ProactiveChatPlugin)
        plugin.config = config
        # 用能力检测结果的替身固定测试新版分支，不依赖运行测试的 AstrBot 版本。
        plugin._astrbot_supports_template_list = lambda: True

        plugin._prepare_group_batches_schema()

        self.assertEqual(config.schema["group_batches"]["type"], "template_list")
        self.assertIn("group_batch", config.schema["group_batches"]["templates"])
        self.assertEqual(config["group_batches"][0]["__template_key"], "group_batch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
