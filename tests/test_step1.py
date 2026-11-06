"""Local Step 1 checks; these do not claim live SDK or API compatibility."""

from __future__ import annotations

import os
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from opsfleet_assistant.config import Settings
from opsfleet_assistant.preflight import (
    MAX_QUERY_BYTES, _error, _run_query, _transient_model_failure,
    main, openrouter_auth_preflight, openrouter_preflight,
)


class ConfigurationTests(unittest.TestCase):
    def test_environment_overrides_files_and_secret_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "GEMINI_API_KEY=private-test-marker\nGOOGLE_CLOUD_PROJECT=file-project\n",
                encoding="utf-8",
            )
            (root / ".env.local").write_text(
                "GOOGLE_CLOUD_PROJECT=local-project\n"
                "OPENROUTER_API_KEY=private-openrouter-marker\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "env-project"}):
                settings = Settings.load(root)
            self.assertEqual(settings.project, "env-project")
            self.assertEqual(settings.gemini_api_key, "private-test-marker")
            self.assertNotIn("private-test-marker", repr(settings))
            self.assertNotIn("private-openrouter-marker", repr(settings))

    def test_error_does_not_include_exception_message(self) -> None:
        result = _error("request_failed", RuntimeError("private-test-marker"))
        self.assertEqual(result, {"status": "request_failed", "error_type": "RuntimeError"})

    def test_error_exposes_only_safe_http_status(self) -> None:
        class ProviderError(Exception):
            code = 503

        result = _error("request_failed", ProviderError("private-test-marker"))
        self.assertEqual(result, {
            "status": "request_failed", "error_type": "ProviderError", "http_status": 503,
        })


class QueryBudgetTests(unittest.TestCase):
    def _fake_sdk(self):
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        bigquery = types.ModuleType("google.cloud.bigquery")

        class QueryJobConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        bigquery.QueryJobConfig = QueryJobConfig
        cloud.bigquery = bigquery
        google.cloud = cloud
        return {"google": google, "google.cloud": cloud, "google.cloud.bigquery": bigquery}

    def test_excessive_dry_run_never_executes(self) -> None:
        class DryJob:
            total_bytes_processed = MAX_QUERY_BYTES + 1

        class Client:
            def __init__(self):
                self.calls = 0

            def query(self, sql, job_config, location, timeout, retry, job_retry):
                self.calls += 1
                self_dry = job_config.kwargs["dry_run"]
                assert self_dry
                assert retry is None and job_retry is None
                return DryJob()

        client = Client()
        with patch.dict(sys.modules, self._fake_sdk()):
            result = _run_query(client, "SELECT 1", "US")
        self.assertEqual(result["status"], "scan_limit")
        self.assertEqual(client.calls, 1)

    def test_execution_carries_byte_and_time_limits(self) -> None:
        class DryJob:
            total_bytes_processed = 100

        class RunJob:
            total_bytes_billed = 100
            job_id = "test-job"

            def result(self, timeout, max_results, retry, job_retry):
                assert timeout <= 35
                assert max_results <= 150
                assert retry is None and job_retry is None
                return []

        class Client:
            def __init__(self):
                self.configs = []

            def query(self, sql, job_config, location, timeout, retry, job_retry):
                assert retry is None and job_retry is None
                self.configs.append(job_config.kwargs)
                return DryJob() if len(self.configs) == 1 else RunJob()

        client = Client()
        with patch.dict(sys.modules, self._fake_sdk()):
            result = _run_query(client, "SELECT 1", "US")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.configs[1]["maximum_bytes_billed"], MAX_QUERY_BYTES)
        self.assertEqual(client.configs[1]["job_timeout_ms"], 30_000)


class ModelFallbackTests(unittest.TestCase):
    def test_only_transient_provider_failures_trigger_fallback(self) -> None:
        self.assertTrue(_transient_model_failure({"status": "request_failed", "http_status": 504}))
        self.assertTrue(_transient_model_failure({"status": "request_failed", "http_status": 429}))
        self.assertFalse(_transient_model_failure({"status": "request_failed", "http_status": 403}))
        self.assertFalse(_transient_model_failure({"status": "unexpected_output"}))

    def test_openrouter_request_requires_structured_output_and_private_route(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"status":"ok"}'}}]}

        class Client:
            def __init__(self, **kwargs):
                self.request = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def post(self, url, headers, json):
                self.request = json
                self.headers = headers
                return Response()

        client = Client()
        settings = Settings(
            project=None, location="US", model="gemini-3.8-flash",
            openrouter_api_key="private-openrouter-marker",
        )
        with patch("httpx.Client", return_value=client):
            result = openrouter_preflight(settings)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.request["response_format"]["type"], "json_schema")
        self.assertEqual(client.request["reasoning"], {"effort": "low"})
        self.assertEqual(client.request["max_tokens"], 512)
        self.assertEqual(client.request["provider"], {
            "require_parameters": True, "zdr": True, "data_collection": "deny",
        })
        self.assertNotIn("private-openrouter-marker", str(result))

    def test_openrouter_reports_token_truncation_without_parsing_content(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{
                    "finish_reason": "length",
                    "message": {"content": '{"st'},
                }]}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def post(self, url, headers, json):
                return Response()

        settings = Settings(
            project=None, location="US", model="gemini-3.8-flash",
            openrouter_api_key="private-openrouter-marker",
        )
        with patch("httpx.Client", return_value=Client()):
            result = openrouter_preflight(settings)
        self.assertEqual(result["status"], "truncated_output")

    def test_models_command_uses_backup_after_504(self) -> None:
        output = io.StringIO()
        with patch.object(sys, "argv", ["preflight", "--models"]), \
             patch("opsfleet_assistant.preflight.gemini_preflight", return_value={
                 "status": "request_failed", "http_status": 504,
             }), \
             patch("opsfleet_assistant.preflight.openrouter_preflight", return_value={
                 "status": "ok", "structured_output": True,
             }) as backup, \
             redirect_stdout(output):
            code = main()
        self.assertEqual(code, 0)
        backup.assert_called_once()
        self.assertIn('"openrouter"', output.getvalue())

    def test_models_command_does_not_use_backup_after_403(self) -> None:
        with patch.object(sys, "argv", ["preflight", "--models"]), \
             patch("opsfleet_assistant.preflight.gemini_preflight", return_value={
                 "status": "request_failed", "http_status": 403,
             }), \
             patch("opsfleet_assistant.preflight.openrouter_preflight") as backup, \
             redirect_stdout(io.StringIO()):
            code = main()
        self.assertEqual(code, 1)
        backup.assert_not_called()

    def test_openrouter_auth_checks_key_without_exposing_response(self) -> None:
        class Response:
            status_code = 401

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def get(self, url, headers):
                self.url = url
                self.headers = headers
                return Response()

        client = Client()
        settings = Settings(
            project=None, location="US", model="gemini-3.8-flash",
            openrouter_api_key="private-openrouter-marker",
        )
        with patch("httpx.Client", return_value=client):
            result = openrouter_auth_preflight(settings)
        self.assertEqual(result, {
            "status": "request_failed", "error_type": "HTTPError", "http_status": 401,
        })
        self.assertEqual(client.url, "https://openrouter.ai/api/v1/key")
        self.assertNotIn("private-openrouter-marker", str(result))

    def test_openrouter_auth_rejects_management_key_for_inference(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                return {"data": {"is_management_key": True}}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def get(self, url, headers):
                return Response()

        settings = Settings(
            project=None, location="US", model="gemini-3.8-flash",
            openrouter_api_key="private-openrouter-marker",
        )
        with patch("httpx.Client", return_value=Client()):
            result = openrouter_auth_preflight(settings)
        self.assertEqual(result, {"status": "wrong_key_type", "required": "inference_api_key"})


if __name__ == "__main__":
    unittest.main()
