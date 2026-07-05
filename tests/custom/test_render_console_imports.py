import py_compile
import unittest


class RenderConsoleImportsTest(unittest.TestCase):
    def test_kurukin_job_adapter_imports_without_scripts_path_hack(self):
        import app.custom.kurukin_job_adapter  # noqa: F401

    def test_kurukin_render_console_imports(self):
        import app.custom.kurukin_render_console  # noqa: F401

    def test_streamlit_page_imports_or_compiles(self):
        py_compile.compile("webui/pages/Kurukin_Render_Console.py", doraise=True)


if __name__ == "__main__":
    unittest.main()
