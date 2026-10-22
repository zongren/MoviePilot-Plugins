"""Standalone configuration tests for the AiSubV2 plugin.

The plugin module (``plugins.v2/aisubv2/__init__.py``) imports MoviePilot and a
few optional third-party packages.  None of them are installed in this
environment, so we install lightweight stubs into :data:`sys.modules` *before*
importing the plugin and then exercise the v2.5 configuration surface.

The MoviePilot ``get_config`` stub intentionally raises ``AssertionError`` for
the ``"ChatGPT"`` key: the plugin no longer depends on the central ChatGPT
configuration and asking for it must fail loudly.

v2.5 moves the settings/data pages to MoviePilot's Vue federation rendering, so
the tests additionally pin the render mode, the shipped ``dist/assets`` build
artifacts and the ``/tasks`` plugin API.

v2.6 adds regression coverage for the task runtime: manual (user triggered)
tasks force regeneration/overwrite of previously generated machine-translated
subtitles, and sidecar subtitle file names without a usable language tag must
not raise ``ValueError`` inside the iso639 lookup.
"""

import json
import pathlib
import queue
import shutil
import sys
import tempfile
import threading
import types
import unittest


REPO = pathlib.Path(__file__).resolve().parents[3]
# The source directory is ``plugins.v2/``, whose name is not a valid Python
# identifier, so the plugin package is imported under its runtime name
# (``aisubv2``) -- the same name MoviePilot uses as ``app.plugins.aisubv2``.
PLUGIN_ROOT = REPO / "plugins.v2"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


# ---------------------------------------------------------------------------
# Observation sinks used by the stubs.
# ---------------------------------------------------------------------------
LOG_RECORDS = []
GET_CONFIG_KEYS = []
POSTED_MESSAGES = []


def _new_module(name, **attrs):
    """Register a fresh :class:`types.ModuleType` under *name*."""
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    sys.modules[name] = module
    return module


def _install_moviepilot_stubs():
    """Install minimal ``app.*`` (MoviePilot) stubs."""

    # -- package skeleton -------------------------------------------------
    app_pkg = _new_module("app")
    core_pkg = _new_module("app.core")
    schemas_pkg = _new_module("app.schemas")
    utils_pkg = _new_module("app.utils")
    app_pkg.core = core_pkg
    app_pkg.schemas = schemas_pkg
    app_pkg.utils = utils_pkg

    # -- app.core.config --------------------------------------------------
    settings = types.SimpleNamespace(
        RMT_MEDIAEXT={".mp4", ".mkv"},
        PROXY={},
    )
    core_pkg.config = _new_module("app.core.config", settings=settings)

    # -- app.core.context -------------------------------------------------
    class MediaInfo:
        pass

    core_pkg.context = _new_module("app.core.context", MediaInfo=MediaInfo)

    # -- app.core.event ---------------------------------------------------
    class Event:
        def __init__(self, event_type=None, event_data=None):
            self.event_type = event_type
            self.event_data = event_data

    class _EventManager:
        def __init__(self):
            self.registrations = []

        def register(self, *args, **kwargs):
            self.registrations.append((args, kwargs))

            def _decorator(func):
                return func

            return _decorator

    core_pkg.event = _new_module(
        "app.core.event", eventmanager=_EventManager(), Event=Event
    )

    # -- app.schemas ------------------------------------------------------
    class TransferInfo:
        pass

    schemas_pkg.TransferInfo = TransferInfo

    class NotificationType:
        Plugin = "Plugin"

    class EventType:
        TransferComplete = "TransferComplete"

    schemas_pkg.types = _new_module(
        "app.schemas.types",
        NotificationType=NotificationType,
        EventType=EventType,
    )

    # -- app.log ----------------------------------------------------------
    class _StubLogger:
        @staticmethod
        def _append(level, message):
            LOG_RECORDS.append((level, str(message)))

        def info(self, message="", *args, **kwargs):
            self._append("info", message)

        def warn(self, message="", *args, **kwargs):
            self._append("warn", message)

        def warning(self, message="", *args, **kwargs):
            self._append("warning", message)

        def error(self, message="", *args, **kwargs):
            self._append("error", message)

        def debug(self, message="", *args, **kwargs):
            self._append("debug", message)

    app_pkg.log = _new_module("app.log", logger=_StubLogger())

    # -- app.utils.system -------------------------------------------------
    class SystemUtils:
        @staticmethod
        def copy(src, dst):
            shutil.copyfile(str(src), str(dst))
            return dst

    utils_pkg.system = _new_module("app.utils.system", SystemUtils=SystemUtils)

    # -- app.plugins ------------------------------------------------------
    class _PluginBase:
        def __init__(self, *args, **kwargs):
            self._stub_data = {}
            self._stub_data_path = None
            self._stub_updated_config = None

        def get_data(self, key, default=None):
            if key in self._stub_data:
                return self._stub_data[key]
            return {} if default is None else default

        def save_data(self, key, value):
            self._stub_data[key] = value

        def update_config(self, cfg):
            self._stub_updated_config = cfg

        def get_data_path(self):
            if self._stub_data_path is None:
                self._stub_data_path = pathlib.Path(
                    tempfile.mkdtemp(prefix="AiSubV2-stub-")
                )
            return self._stub_data_path

        def post_message(self, **kwargs):
            POSTED_MESSAGES.append(kwargs)

        def get_config(self, key):
            GET_CONFIG_KEYS.append(key)
            if key == "ChatGPT":
                raise AssertionError(
                    "插件不应再读取 MoviePilot 的 ChatGPT 配置（已改为独立大模型配置）"
                )
            return None

    app_pkg.plugins = _new_module("app.plugins", _PluginBase=_PluginBase)


def _install_third_party_stubs():
    """Install stubs for the optional dependencies the plugin imports."""
    if "iso639" not in sys.modules:

        class NonExistentLanguageError(ValueError):
            pass

        _new_module(
            "iso639",
            find=lambda name: name or None,
            to_iso639_1=lambda name: name,
            NonExistentLanguageError=NonExistentLanguageError,
        )

    if "psutil" not in sys.modules:
        _new_module("psutil", cpu_count=lambda logical=True: 4)

    if "srt" not in sys.modules:
        _new_module(
            "srt",
            Subtitle=object,
            parse=lambda *args, **kwargs: [],
            compose=lambda *args, **kwargs: "",
        )

    if "lxml" not in sys.modules:

        class _FakeHtmlElement:
            def xpath(self, expression):
                return ""

        lxml_module = _new_module("lxml")
        lxml_module.etree = _new_module(
            "lxml.etree", HTML=lambda *args, **kwargs: _FakeHtmlElement()
        )


_install_moviepilot_stubs()
_install_third_party_stubs()

import aisubv2 as plugin_module  # noqa: E402  (import after stubbing)


PLUGIN_API_MODELS = [
    "llm_api_type",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_reasoning_effort",
    "llm_max_tokens",
    "max_retries",
    "zh_only",
    "ignore_if_chinese_exists",
]

REMOVED_CONFIG_KEYS = ["enable_batch", "batch_size", "context_window", "enable_merge"]


def _iter_model_props(node):
    """Yield every ``props`` dict that declares a ``model`` key."""
    if isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict) and "model" in props:
            yield props
        for value in node.values():
            yield from _iter_model_props(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_model_props(item)


class AiSubV2PluginConfigTest(unittest.TestCase):
    def setUp(self):
        LOG_RECORDS.clear()
        GET_CONFIG_KEYS.clear()
        POSTED_MESSAGES.clear()
        self.plugin = plugin_module.AiSubV2()

    # -- 1. version -------------------------------------------------------
    def test_plugin_version_is_2_6(self):
        self.assertEqual(plugin_module.AiSubV2.plugin_version, "2.6")

    # -- 2. defaults + removed keys --------------------------------------
    def test_get_form_defaults_and_removed_keys(self):
        form = self.plugin.get_form()
        self.assertIsInstance(form, tuple)
        self.assertEqual(len(form), 2)
        schema, defaults = form

        expected_defaults = {
            "llm_api_type": "openai_chat",
            "llm_base_url": "",
            "llm_api_key": "",
            "llm_model": "",
            "llm_reasoning_effort": "high",
            "llm_max_tokens": 64000,
            "max_retries": 3,
            "zh_only": False,
            "ignore_if_chinese_exists": True,
        }
        for key, value in expected_defaults.items():
            self.assertIn(key, defaults, f"默认配置缺少 {key}")
            self.assertEqual(defaults[key], value, f"{key} 默认值不符")
            self.assertIs(
                type(defaults[key]), type(value), f"{key} 默认值类型不符"
            )

        for key in REMOVED_CONFIG_KEYS:
            self.assertNotIn(key, defaults, f"已废弃的配置项不应出现在默认值中：{key}")

        schema_models = {props["model"] for props in _iter_model_props(schema)}
        for key in REMOVED_CONFIG_KEYS:
            self.assertNotIn(key, schema_models, f"已废弃的配置项不应出现在表单中：{key}")

    # -- 3. schema exposes the LLM surface -------------------------------
    def test_llm_schema_exposes_required_models(self):
        schema, _defaults = self.plugin.get_form()
        model_props = {}
        for props in _iter_model_props(schema):
            model_props[props["model"]] = props

        for model in PLUGIN_API_MODELS:
            self.assertIn(model, model_props, f"配置表单缺少字段 {model}")

        self.assertEqual(
            model_props["llm_api_key"].get("type"),
            "password",
            "llm_api_key 应为 password 类型",
        )

    # -- 4. openai_chat wiring + no ChatGPT dependency --------------------
    def test_translate_zh_wires_openai_chat_provider(self):
        self.plugin.init_plugin(
            {
                "enabled": False,
                "enable_asr": False,
                "translate_zh": True,
                "llm_base_url": "https://api.deepseek.com",
                "llm_api_key": "sk-test",
                "llm_model": "deepseek-flash",
            }
        )

        self.assertIsNotNone(self.plugin._llm_provider)
        self.assertEqual(
            type(self.plugin._llm_provider).__name__, "OpenAIChatProvider"
        )
        # The plugin must not consult the legacy central ChatGPT config.
        self.assertNotIn("ChatGPT", GET_CONFIG_KEYS)
        # ...and the guard stub really would have failed loudly.
        with self.assertRaises(AssertionError):
            self.plugin.get_config("ChatGPT")

    # -- 5. other provider types ------------------------------------------
    def test_translate_zh_wires_alternative_providers(self):
        cases = {
            "anthropic_messages": "AnthropicMessagesProvider",
            "openai_responses": "OpenAIResponsesProvider",
        }
        for api_type, class_name in cases.items():
            with self.subTest(api_type=api_type):
                plugin = plugin_module.AiSubV2()
                plugin.init_plugin(
                    {
                        "enabled": False,
                        "enable_asr": False,
                        "translate_zh": True,
                        "llm_api_type": api_type,
                        "llm_base_url": "https://api.example.com",
                        "llm_api_key": "sk-test",
                        "llm_model": "some-model",
                    }
                )
                self.assertIsNotNone(plugin._llm_provider)
                self.assertEqual(
                    type(plugin._llm_provider).__name__, class_name
                )
                self.assertNotIn("ChatGPT", GET_CONFIG_KEYS)

    # -- 6. blank credentials degrade gracefully --------------------------
    def test_blank_llm_api_key_keeps_provider_none(self):
        self.plugin.init_plugin(
            {
                "enabled": False,
                "enable_asr": False,
                "translate_zh": True,
                "llm_base_url": "https://api.deepseek.com",
                "llm_api_key": "",
                "llm_model": "deepseek-flash",
            }
        )

        self.assertIsNone(
            self.plugin._llm_provider,
            "缺少 llm_api_key 时不应创建 provider",
        )
        errors = [message for level, message in LOG_RECORDS if level == "error"]
        self.assertTrue(
            any("llm" in message or "大模型" in message for message in errors),
            f"应记录配置错误日志，实际日志：{LOG_RECORDS}",
        )

    # -- 7. zh_only / ignore_if_chinese_exists ----------------------------
    def test_zh_only_and_ignore_defaults_when_omitted(self):
        self.plugin.init_plugin(
            {"enabled": False, "enable_asr": False, "translate_zh": False}
        )
        self.assertIs(self.plugin._zh_only, False)
        self.assertIs(self.plugin._ignore_if_chinese_exists, True)

    def test_zh_only_and_ignore_explicit_values(self):
        self.plugin.init_plugin(
            {
                "enabled": False,
                "enable_asr": False,
                "translate_zh": False,
                "zh_only": True,
                "ignore_if_chinese_exists": False,
            }
        )
        self.assertIs(self.plugin._zh_only, True)
        self.assertIs(self.plugin._ignore_if_chinese_exists, False)

    # -- 8. regression: incomplete config must still stop the service ------
    def test_incomplete_llm_config_still_stops_service(self):
        stopped = []
        self.plugin.stop_service = lambda: stopped.append(True)
        self.plugin.init_plugin(
            {
                "enabled": False,
                "enable_asr": False,
                "translate_zh": True,
                "llm_base_url": "https://api.deepseek.com",
                "llm_api_key": "",
                "llm_model": "deepseek-flash",
            }
        )
        self.assertTrue(stopped, "配置不完整时也必须执行停止流程，避免残留消费线程")
        self.assertIsNone(self.plugin._llm_provider)

    # -- 9. regression: stale provider must be cleared on re-init ---------
    def test_llm_provider_is_reset_on_reinit(self):
        valid = {
            "enabled": False,
            "enable_asr": False,
            "translate_zh": True,
            "llm_base_url": "https://api.deepseek.com",
            "llm_api_key": "sk-test",
            "llm_model": "deepseek-flash",
        }
        self.plugin.init_plugin(dict(valid))
        self.assertIsNotNone(self.plugin._llm_provider)

        self.plugin.init_plugin({"enabled": False, "enable_asr": False, "translate_zh": False})
        self.assertIsNone(self.plugin._llm_provider, "关闭翻译后应清空旧客户端")

        self.plugin.init_plugin(dict(valid))
        self.assertIsNotNone(self.plugin._llm_provider)
        bad = dict(valid)
        bad["llm_api_key"] = ""
        self.plugin.init_plugin(bad)
        self.assertIsNone(self.plugin._llm_provider, "配置失效后不应保留旧客户端")

    # -- 10. regression: V2 index/layout must match the host contract ----
    def test_v2_index_and_layout_match_host_contract(self):
        """
        MoviePilot v2 resolves the plugin files as
        ``contents/plugins[.{package_version}]/{pid.lower()}``; the pid was
        found in ``package.v2.json`` (not ``package.json``), so the code must
        live in ``plugins.v2/aisubv2`` and both indexes must agree.
        """
        import json

        index_v2 = json.loads((REPO / "package.v2.json").read_text(encoding="utf-8"))
        self.assertIn("AiSubV2", index_v2)
        self.assertEqual(
            index_v2["AiSubV2"]["version"],
            plugin_module.AiSubV2.plugin_version,
            "package.v2.json 的 version 必须与 plugin_version 一致",
        )
        self.assertIn(
            f"v{plugin_module.AiSubV2.plugin_version}",
            index_v2["AiSubV2"].get("history", {}),
            "history 必须包含当前版本的变更说明",
        )

        # dir name == class name lowercased == pid.lower()
        expected_dir = REPO / "plugins.v2" / plugin_module.AiSubV2.__name__.lower()
        self.assertTrue((expected_dir / "__init__.py").is_file(), f"缺少插件目录 {expected_dir}")
        self.assertEqual(expected_dir.name, "aisubv2")

        # The v1 index must not advertise this V2-only plugin: the host would
        # resolve it to ``plugins/aisubv2`` (v1 root) and 404.
        index_v1 = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
        self.assertNotIn("AiSubV2", index_v1)

    # -- 11. Vue federation rendering contract ----------------------------
    def test_render_mode_uses_vue_federation(self):
        """配置页/数据页必须声明 Vue 联邦渲染，并随包分发编译产物。"""
        self.assertEqual(
            plugin_module.AiSubV2.get_render_mode(),
            ("vue", "dist/assets"),
        )

        plugin_dir = REPO / "plugins.v2" / plugin_module.AiSubV2.__name__.lower()
        remote_entry = plugin_dir / "dist" / "assets" / "remoteEntry.js"
        self.assertTrue(
            remote_entry.is_file(),
            f"缺少前端联邦产物 {remote_entry}，需要先执行 npm run build",
        )

        remote_source = remote_entry.read_text(encoding="utf-8")
        for expose in ("./Config", "./Page"):
            self.assertIn(expose, remote_source, f"remoteEntry.js 未暴露 {expose}")

        # 暴露的组件文件必须真实存在，否则宿主加载时会 404
        for asset in ("__federation_expose_Config", "__federation_expose_Page"):
            matches = list((plugin_dir / "dist" / "assets").glob(f"{asset}-*.js"))
            self.assertTrue(matches, f"dist/assets 缺少 {asset} 产物")

    def test_frontend_build_project_exposes_required_components(self):
        """前端构建工程需保留 Vue 侧源码与联邦 exposes 配置。"""
        plugin_dir = REPO / "plugins.v2" / plugin_module.AiSubV2.__name__.lower()

        vite_config = (plugin_dir / "vite.config.js").read_text(encoding="utf-8")
        self.assertIn("'./Config'", vite_config)
        self.assertIn("'./Page'", vite_config)
        self.assertIn("remoteEntry.js", vite_config)

        for relative in (
            "src/components/Config.vue",
            "src/components/Page.vue",
            "src/components/FilePickerDialog.vue",
            "src/components/JsonFormNode.vue",
        ):
            self.assertTrue((plugin_dir / relative).is_file(), f"缺少前端源码 {relative}")

    # -- 12. picker marker + /tasks API -----------------------------------
    def test_path_list_field_is_marked_for_file_picker(self):
        """媒体路径输入框需要带 picker 标记，Vue 配置页据此渲染选择按钮。"""
        schema, _defaults = self.plugin.get_form()
        marked = []

        def walk(node):
            if isinstance(node, dict):
                props = node.get("props")
                if isinstance(props, dict) and props.get("model") == "path_list":
                    marked.append(node.get("picker"))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(schema)
        self.assertEqual(marked, ["path_list"], "path_list 输入框应且仅应带一个 picker 标记")

    def test_get_api_registers_bear_tasks_route(self):
        """Vue 数据页需要一个 bear 鉴权的任务查询接口。"""
        routes = self.plugin.get_api()
        self.assertIsInstance(routes, list)
        self.assertEqual(len(routes), 1)

        route = routes[0]
        self.assertEqual(route["path"], "/tasks")
        self.assertEqual(route["methods"], ["GET"])
        self.assertEqual(route["auth"], "bear")
        self.assertTrue(callable(route["endpoint"]))

    def test_tasks_api_payload_serializes_queue(self):
        """任务接口返回前端表格所需的字段与状态统计。"""
        self.plugin._tasks = {}
        self.plugin._task_queue = queue.Queue()
        self.assertTrue(
            self.plugin.add_task("/media/movie/a.mkv", plugin_module.TaskSource.MANUAL)
        )
        self.assertTrue(
            self.plugin.add_task("/media/movie/b.mkv", plugin_module.TaskSource.EVENT)
        )

        payload = self.plugin.get_tasks_api()
        self.assertTrue(payload["success"])
        data = payload["data"]
        self.assertFalse(data["running"])

        tasks = data["tasks"]
        self.assertEqual(len(tasks), 2)
        by_file = {task["video_file"]: task for task in tasks}
        self.assertEqual(by_file["/media/movie/a.mkv"]["source_label"], "手动添加")
        self.assertEqual(by_file["/media/movie/b.mkv"]["source_label"], "入库触发")
        for task in tasks:
            self.assertEqual(task["status"], "pending")
            self.assertEqual(task["status_text"], "等待中")
            self.assertEqual(task["complete_time"], "-")
            self.assertEqual(task["status_class"], "text-info")

        summary = data["summary"]
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["pending"], 2)
        for status in ("in_progress", "completed", "ignored", "failed"):
            self.assertEqual(summary[status], 0)

    def test_json_page_fallback_remains_available(self):
        """旧版本 MoviePilot 仍会走 JSON 数据页，需保持可用。"""
        self.plugin._tasks = {}
        self.plugin._task_queue = queue.Queue()
        self.plugin.add_task("/media/movie/a.mkv", plugin_module.TaskSource.MANUAL)

        page = self.plugin.get_page()
        self.assertIsInstance(page, list)
        self.assertTrue(page)


class _RealisticIso639:
    """``iso639`` stand-in that mirrors the real library's error behaviour.

    The real ``iso639.to_iso639_1`` raises ``ValueError('Invalid search
    criteria.')`` for empty keys (e.g. a sidecar file named ``Movie.srt``) and
    ``NonExistentLanguageError`` for unknown ones.  Neither may escape a
    subtitle task.
    """

    class NonExistentLanguageError(RuntimeError):
        pass

    _CODES = {
        "en": "en",
        "eng": "en",
        "english": "en",
        "zh": "zh",
        "chi": "zh",
        "zho": "zh",
        "chinese": "zh",
        "de": "de",
        "ger": "de",
        "deu": "de",
        "ja": "ja",
        "jpn": "ja",
        "und": "",  # real library: language without a two-letter code
    }

    @classmethod
    def to_iso639_1(cls, key):
        if not key:
            raise ValueError("Invalid search criteria.")
        code = cls._CODES.get(str(key).strip().lower())
        if code is None:
            raise cls.NonExistentLanguageError("Language does not exist.")
        return code


class AiSubV2LanguageParsingTest(unittest.TestCase):
    """v2.6 regression: sidecar names must never crash the language parser."""

    def setUp(self):
        LOG_RECORDS.clear()
        self._original_iso639 = plugin_module.iso639
        plugin_module.iso639 = _RealisticIso639
        self.addCleanup(self._restore_iso639)
        self.tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="aisubv2-sidecar-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _restore_iso639(self):
        plugin_module.iso639 = self._original_iso639

    def _video(self, name="校园大逃杀2：篝火行动 (2026) - 2160p.mkv"):
        path = self.tmpdir / name
        path.write_bytes(b"video")
        return str(path)

    def _sidecar(self, video, suffix):
        video_path = pathlib.Path(video)
        sidecar = video_path.with_name(video_path.stem + suffix)
        sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        return sidecar

    @staticmethod
    def _match(video, prefer_langs, **kwargs):
        return plugin_module.AiSubV2._AiSubV2__external_subtitle_exists(
            video, prefer_langs, only_srt=True, strict=False, **kwargs
        )

    def test_untagged_sidecar_does_not_raise_and_is_ignored(self):
        """文件名缺少语言标记（如 ``影片.mkv`` 旁的 ``影片.srt``）不应再抛 ValueError。"""
        video = self._video()
        self._sidecar(video, ".srt")
        exists, lang, name = self._match(video, ["en", "eng"])
        self.assertFalse(exists)
        self.assertIsNone(lang)
        self.assertIsNone(name)

    def test_untagged_sidecar_does_not_block_tagged_one(self):
        video = self._video()
        self._sidecar(video, ".srt")
        tagged = self._sidecar(video, ".eng.srt")
        exists, lang, name = self._match(video, ["en", "eng"])
        self.assertTrue(exists)
        self.assertEqual(lang, "en")
        self.assertEqual(name, tagged.name)

    def test_sidecar_with_empty_props_segment_is_ignored(self):
        video = self._video()
        self._sidecar(video, "..srt")
        exists, _, _ = self._match(video, ["en", "eng"])
        self.assertFalse(exists)

    def test_non_language_sidecar_props_are_ignored(self):
        video = self._video()
        self._sidecar(video, ".2160p.srt")
        exists, _, _ = self._match(video, ["en", "eng"])
        self.assertFalse(exists)

    def test_generated_translation_is_excluded_as_source(self):
        """插件自己生成的机翻字幕不能作为下一次翻译的源字幕。"""
        video = self._video()
        translated = self._sidecar(video, ".zh.机翻.srt")
        exists, _, _ = self._match(video, ["en", "eng"], exclude_translated=True)
        self.assertFalse(exists)
        # 默认行为不变：查询“是否存在已有字幕”时仍能识别到该文件
        exists, lang, name = self._match(video, ["en", "eng"])
        self.assertTrue(exists)
        self.assertEqual(lang, "zh")
        self.assertEqual(name, translated.name)

    def test_translation_excluded_but_english_source_found(self):
        video = self._video()
        self._sidecar(video, ".zh.机翻.srt")
        tagged = self._sidecar(video, ".en.srt")
        exists, lang, name = self._match(video, ["en", "eng"], exclude_translated=True)
        self.assertTrue(exists)
        self.assertEqual((lang, name), ("en", tagged.name))

    def test_to_iso639_1_is_tolerant_of_invalid_input(self):
        self.assertIsNone(plugin_module._to_iso639_1(""))
        self.assertIsNone(plugin_module._to_iso639_1(None))
        self.assertIsNone(plugin_module._to_iso639_1("机翻"))
        self.assertIsNone(plugin_module._to_iso639_1("und"))
        self.assertEqual(plugin_module._to_iso639_1("eng"), "en")
        self.assertEqual(plugin_module._to_iso639_1(" English "), "en")


class AiSubV2ManualOverwriteTest(unittest.TestCase):
    """v2.6: manually triggered tasks force regeneration/overwrite."""

    def setUp(self):
        LOG_RECORDS.clear()
        self.plugin = plugin_module.AiSubV2()
        self.plugin._file_size = 0
        self.plugin._enable_asr = False
        self.plugin._translate_zh = False
        self.plugin._send_notify = False
        self.plugin._ignore_if_chinese_exists = True
        self.tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="aisubv2-force-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.video = self.tmpdir / "校园大逃杀2：篝火行动 (2026) - 2160p.mkv"
        self.video.write_bytes(b"video")
        self.generated = []
        self.translated = []
        self.plugin._AiSubV2__generate_subtitle = self._fake_generate
        self.plugin._AiSubV2__translate_zh_subtitle = self._fake_translate

    def _fake_generate(self, video_file, subtitle_file, enable_asr=True):
        self.generated.append((video_file, subtitle_file))
        return True, "en", f"{subtitle_file}.en.srt"

    def _fake_translate(self, source_lang, source_subtitle, dest_subtitle):
        self.translated.append((source_lang, source_subtitle, dest_subtitle))
        pathlib.Path(dest_subtitle).write_text("translated", encoding="utf-8")
        return types.SimpleNamespace(unrecovered=0)

    def _run(self, force):
        return self.plugin._AiSubV2__process_autosub(str(self.video), force=force)

    def test_manual_force_overwrites_machine_translation(self):
        existing = self.tmpdir / "校园大逃杀2：篝火行动 (2026) - 2160p.zh.机翻.srt"
        existing.write_text("旧机翻内容", encoding="utf-8")
        self.plugin._translate_zh = True
        self.plugin._AiSubV2__chinese_subtitle_exists = lambda video_file: (True, True)

        status = self._run(force=True)

        self.assertEqual(status, plugin_module.TaskStatus.COMPLETED)
        self.assertEqual(len(self.generated), 1, "手动执行应重新生成字幕")
        self.assertEqual(len(self.translated), 1, "手动执行应重新翻译字幕")
        self.assertEqual(self.translated[0][2], str(existing))
        self.assertEqual(existing.read_text(encoding="utf-8"), "translated", "旧机翻字幕应被覆盖")

    def test_auto_task_still_skips_when_machine_translation_exists(self):
        self.plugin._AiSubV2__chinese_subtitle_exists = lambda video_file: (True, True)

        status = self._run(force=False)

        self.assertEqual(status, plugin_module.TaskStatus.IGNORED)
        self.assertEqual(self.generated, [])
        self.assertEqual(self.translated, [])

    def test_manual_force_still_skips_real_chinese_subtitle(self):
        """真实中文字幕不是“翻译字幕”，手动执行也不应无谓重做。"""
        self.plugin._AiSubV2__chinese_subtitle_exists = lambda video_file: (True, False)

        status = self._run(force=True)

        self.assertEqual(status, plugin_module.TaskStatus.IGNORED)
        self.assertEqual(self.generated, [])

    def test_manual_force_bypasses_existing_target_subtitle_check(self):
        self.plugin._AiSubV2__chinese_subtitle_exists = lambda video_file: (False, False)
        self.plugin._AiSubV2__target_subtitle_exists = lambda video_file: True

        self.assertEqual(self._run(force=True), plugin_module.TaskStatus.COMPLETED)
        self.assertEqual(len(self.generated), 1)

    def test_auto_task_skips_existing_target_subtitle(self):
        self.plugin._AiSubV2__chinese_subtitle_exists = lambda video_file: (False, False)
        self.plugin._AiSubV2__target_subtitle_exists = lambda video_file: True

        self.assertEqual(self._run(force=False), plugin_module.TaskStatus.IGNORED)
        self.assertEqual(self.generated, [])

    def test_machine_translation_detection(self):
        original = plugin_module.chinese.find_external_chinese_subtitle
        self.addCleanup(setattr, plugin_module.chinese, "find_external_chinese_subtitle", original)
        plugin = plugin_module.AiSubV2()

        plugin_module.chinese.find_external_chinese_subtitle = (
            lambda video_file: (True, "Movie (2026).zh.机翻.srt")
        )
        self.assertEqual(plugin._AiSubV2__chinese_subtitle_exists("/media/Movie (2026).mkv"), (True, True))

        plugin_module.chinese.find_external_chinese_subtitle = (
            lambda video_file: (True, "Movie (2026).chs.srt")
        )
        self.assertEqual(plugin._AiSubV2__chinese_subtitle_exists("/media/Movie (2026).mkv"), (True, False))


class AiSubV2TaskQueueTest(unittest.TestCase):
    """v2.6: task source is converted into the force flag of a task run."""

    def _run_single_task(self, source):
        plugin = plugin_module.AiSubV2()
        plugin._tasks = {}
        plugin._task_queue = queue.Queue()
        plugin._event = threading.Event()
        calls = []

        def fake_process(video_file, force=False):
            calls.append((video_file, force))
            plugin._event.set()  # 让消费线程在本次任务后退出
            return plugin_module.TaskStatus.COMPLETED

        plugin._AiSubV2__process_autosub = fake_process
        plugin.add_task("/media/movie/a.mkv", source)
        plugin._consume_tasks()
        return calls

    def test_manual_task_forces_overwrite(self):
        self.assertEqual(
            self._run_single_task(plugin_module.TaskSource.MANUAL),
            [("/media/movie/a.mkv", True)],
        )

    def test_transfer_event_task_is_not_forced(self):
        self.assertEqual(
            self._run_single_task(plugin_module.TaskSource.EVENT),
            [("/media/movie/a.mkv", False)],
        )


if __name__ == "__main__":
    unittest.main()
