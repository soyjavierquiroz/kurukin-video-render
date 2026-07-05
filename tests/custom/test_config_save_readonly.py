import errno
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_MODULE_PATH = PROJECT_ROOT / "app" / "config" / "config.py"


class _FakeLogger:
    def __init__(self):
        self.warnings = []

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, message, *_args, **_kwargs):
        self.warnings.append(message)


def load_isolated_config_module():
    fake_logger = _FakeLogger()
    fake_toml = types.SimpleNamespace(
        load=lambda _path: {"app": {}, "ui": {}, "log_level": "DEBUG"},
        loads=lambda _content: {"app": {}, "ui": {}, "log_level": "DEBUG"},
        dumps=lambda value: repr(value),
    )
    fake_loguru = types.SimpleNamespace(logger=fake_logger)
    module_name = "_test_moneyprinterturbo_config"
    original_toml = sys.modules.get("toml")
    original_loguru = sys.modules.get("loguru")
    original_test_module = sys.modules.get(module_name)
    sys.modules["toml"] = fake_toml
    sys.modules["loguru"] = fake_loguru
    try:
        spec = importlib.util.spec_from_file_location(module_name, CONFIG_MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module._test_logger = fake_logger
        return module
    finally:
        if original_toml is None:
            sys.modules.pop("toml", None)
        else:
            sys.modules["toml"] = original_toml
        if original_loguru is None:
            sys.modules.pop("loguru", None)
        else:
            sys.modules["loguru"] = original_loguru
        if original_test_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = original_test_module


class TestConfigSaveReadonly(unittest.TestCase):
    def test_save_config_returns_true_when_writable(self):
        config = load_isolated_config_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            config.config_file = str(Path(tmp_dir) / "config.toml")

            result = config.save_config()

            self.assertTrue(result)
            self.assertTrue(Path(config.config_file).is_file())

    def test_save_config_skips_readonly_filesystem(self):
        config = load_isolated_config_module()
        with mock.patch("builtins.open", side_effect=OSError(errno.EROFS, "readonly")):
            self.assertFalse(config.save_config())
        self.assertIn("skipping save_config", config._test_logger.warnings[0])

    def test_save_config_skips_access_denied(self):
        config = load_isolated_config_module()
        with mock.patch("builtins.open", side_effect=OSError(errno.EACCES, "denied")):
            self.assertFalse(config.save_config())

    def test_save_config_reraises_unexpected_oserror(self):
        config = load_isolated_config_module()
        with mock.patch("builtins.open", side_effect=OSError(errno.EIO, "io error")):
            with self.assertRaises(OSError):
                config.save_config()


if __name__ == "__main__":
    unittest.main()
