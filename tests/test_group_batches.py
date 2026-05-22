"""群聊批次配置路由单元测试。"""

import copy
import json
import sys
import unittest
from pathlib import Path

# 把插件目录加入路径
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# Mock astrbot.api.logger
class MockLogger:
    @staticmethod
    def debug(*args, **kwargs): pass
    @staticmethod
    def info(*args, **kwargs): pass
    @staticmethod
    def warning(*args, **kwargs): pass
    @staticmethod
    def error(*args, **kwargs): pass

sys.modules["astrbot"] = type(sys)("astrbot")
sys.modules["astrbot.api"] = type(sys)("astrbot.api")
sys.modules["astrbot.api"].logger = MockLogger()

from core.session_config import ConfigMixin


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


class TestConfigMixin(ConfigMixin, MockSessionParser):
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
        return TestConfigMixin(config)

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

    def test_group_disabled_no_batch(self):
        """全局群聊关闭时，批次也不生效。"""
        config = self._make_config().config
        config["group_settings"]["enable"] = False
        mixin = TestConfigMixin(config)
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
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)

        self.assertIn("group_batches", schema)
        self.assertEqual(schema["group_batches"]["type"], "template_list")
        templates = schema["group_batches"]["templates"]
        self.assertIn("group_batch", templates)
        items = templates["group_batch"]["items"]
        self.assertIn("batch_name", items)
        self.assertIn("session_list", items)
        self.assertIn("min_interval_minutes", items)
        self.assertIn("max_interval_minutes", items)
        self.assertIn("quiet_hours", items)
        self.assertIn("max_unanswered_times", items)
        self.assertIn("proactive_prompt", items)


if __name__ == "__main__":
    unittest.main(verbosity=2)
