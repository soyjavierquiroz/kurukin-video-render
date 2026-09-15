import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.atlas_runtime import (
    AtlasRuntimeConfigurationError,
    build_atlas_runtime_from_env,
)
from app.custom.material_discovery import discover_material_candidates
from app.custom.material_source_policy import (
    MaterialProviderPolicy,
    MaterialSourcePolicy,
    PROVIDER_ATLAS,
    PROVIDER_PEXELS,
    open_sources_policy,
)


class TestAtlasRuntime(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertIsNone(build_atlas_runtime_from_env({}))

    def test_explicit_false_is_disabled(self):
        self.assertIsNone(build_atlas_runtime_from_env({"ATLAS_ENABLED": " false "}))

    def test_explicit_true_requires_base_url(self):
        with self.assertRaisesRegex(AtlasRuntimeConfigurationError, "ATLAS_BASE_URL"):
            build_atlas_runtime_from_env({"ATLAS_ENABLED": "true"})

    def test_invalid_boolean_is_rejected(self):
        with self.assertRaisesRegex(AtlasRuntimeConfigurationError, "ATLAS_ENABLED"):
            build_atlas_runtime_from_env({"ATLAS_ENABLED": "1"})

    def test_invalid_timeout_is_rejected(self):
        for timeout in ("", "0", "-1", "nan", "inf", "soon"):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                AtlasRuntimeConfigurationError, "ATLAS_TIMEOUT_SECONDS"
            ):
                build_atlas_runtime_from_env({
                    "ATLAS_ENABLED": "true",
                    "ATLAS_BASE_URL": "http://127.0.0.1:18765",
                    "ATLAS_TIMEOUT_SECONDS": timeout,
                })

    def test_invalid_base_url_is_a_configuration_error(self):
        with self.assertRaisesRegex(AtlasRuntimeConfigurationError, "ATLAS_BASE_URL"):
            build_atlas_runtime_from_env({
                "ATLAS_ENABLED": "true",
                "ATLAS_BASE_URL": "not-an-url",
            })

    def test_enabled_builds_one_shared_client(self):
        runtime = build_atlas_runtime_from_env({
            "ATLAS_ENABLED": "true",
            "ATLAS_BASE_URL": "http://127.0.0.1:18765/",
            "ATLAS_TIMEOUT_SECONDS": "15",
        })
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.client.base_url, "http://127.0.0.1:18765")
        self.assertEqual(runtime.client.timeout_seconds, 15.0)
        self.assertIs(runtime.provider.client, runtime.client)
        self.assertIs(runtime.materializer.client, runtime.client)

    def test_runtime_does_not_add_atlas_to_existing_policies(self):
        self.assertNotIn(PROVIDER_ATLAS, open_sources_policy().providers.enabled)
        policy = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_PEXELS,)))
        self.assertNotIn(PROVIDER_ATLAS, policy.providers.enabled)

    def test_atlas_policy_without_runtime_reports_missing_capability(self):
        policy = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_ATLAS, PROVIDER_PEXELS)))
        result = discover_material_candidates(
            policy=policy,
            stock_terms=["quiet street"],
            atlas_scope={"kind": "title", "title_id": "explicit-title"},
        )
        self.assertEqual(result.diagnostics[0].provider, PROVIDER_ATLAS)
        self.assertEqual(result.diagnostics[0].status, "config_missing")

    def test_no_scope_is_inferred_from_policy_or_query_text(self):
        policy = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_ATLAS, PROVIDER_PEXELS)))
        result = discover_material_candidates(
            policy=policy,
            stock_terms=["display title must not become scope"],
        )
        self.assertEqual(result.diagnostics[0].provider, PROVIDER_ATLAS)
        self.assertEqual(result.diagnostics[0].status, "config_missing")


if __name__ == "__main__":
    unittest.main()
