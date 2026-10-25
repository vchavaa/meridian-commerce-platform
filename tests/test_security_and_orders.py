import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"
sys.path.insert(0, str(APPS / "order-service"))
sys.path.insert(0, str(APPS))

from common.admin_auth import authorize_admin
from common.secret_config import get_secret_value


def load_app(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storefront = load_app("storefront_test_app", "apps/storefront-ui/app.py")
orders = load_app("order_test_app", "apps/order-service/app.py")


class AdminAuthorizationTests(unittest.TestCase):
    def test_local_token_is_required_and_role_headers_are_ignored(self):
        with patch.dict(os.environ, {"APP_ENV": "local", "LOCAL_ADMIN_TOKEN": "demo-token"}):
            self.assertEqual(authorize_admin({"X-IAM-Role": "sre"}), (401, None))
            self.assertEqual(
                authorize_admin({"Authorization": "Bearer demo-token"}),
                (200, "local-demo-operator"),
            )
            self.assertEqual(
                authorize_admin({"Authorization": "Bearer wrong-token", "X-IAM-Role": "sre"}),
                (401, None),
            )

    def test_local_token_can_be_read_from_a_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "local_admin_token"
            token_path.write_text("file-token\n", encoding="utf-8")
            with patch.dict(os.environ, {
                "APP_ENV": "local",
                "LOCAL_ADMIN_TOKEN_FILE": str(token_path),
            }, clear=True):
                self.assertEqual(
                    authorize_admin({"Authorization": "Bearer file-token"}),
                    (200, "local-demo-operator"),
                )

    def test_google_identity_requires_verified_allowlisted_email(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "GOOGLE_OIDC_AUDIENCE": "https://store.example",
            "ADMIN_ALLOWED_EMAILS": "ops@example.com",
        }):
            with patch(
                "common.admin_auth.id_token.verify_oauth2_token",
                return_value={"email": "ops@example.com", "email_verified": True},
            ):
                self.assertEqual(
                    authorize_admin({"Authorization": "Bearer signed-token"}),
                    (200, "ops@example.com"),
                )
            with patch(
                "common.admin_auth.id_token.verify_oauth2_token",
                return_value={"email": "other@example.com", "email_verified": True},
            ):
                self.assertEqual(
                    authorize_admin({"Authorization": "Bearer signed-token"}),
                    (403, None),
                )
            with patch(
                "common.admin_auth.id_token.verify_oauth2_token",
                return_value={"email": "ops@example.com", "email_verified": False},
            ):
                self.assertEqual(
                    authorize_admin({"Authorization": "Bearer signed-token"}),
                    (403, None),
                )


class EndpointSecurityTests(unittest.TestCase):
    def test_drain_requires_authorization_and_boolean_post_body(self):
        with patch.dict(os.environ, {"APP_ENV": "local", "LOCAL_ADMIN_TOKEN": "demo-token"}):
            status, _, _ = storefront.process_request(
                "POST", "/api/admin/drain", headers={}, body_bytes=b'{"drain":true}'
            )
            self.assertEqual(status, 401)

            status, _, _ = storefront.process_request(
                "POST", "/api/admin/drain",
                headers={"Authorization": "Bearer demo-token"},
                body_bytes=b'{"drain":"true"}',
            )
            self.assertEqual(status, 400)

            with patch.object(storefront.region_state, "set_drained") as set_drained:
                status, _, _ = storefront.process_request(
                    "POST", "/api/admin/drain",
                    headers={"Authorization": "Bearer demo-token"},
                    body_bytes=b'{"drain":true}',
                )
            self.assertEqual(status, 200)
            set_drained.assert_called_once_with(storefront.CLUSTER_REGION, True)

    def test_order_ledger_is_not_public_and_order_input_is_validated(self):
        status, _, _ = orders.process_request("GET", "/api/orders")
        self.assertEqual(status, 401)

        status, _, _ = orders.process_request(
            "POST", "/api/orders", body_bytes=b'{"sku":"MRD-101","quantity":0}'
        )
        self.assertEqual(status, 400)

    def test_order_persistence_receives_true_client_ip(self):
        client_ip = "198.51.100.77"
        with patch.dict(os.environ, {"APP_ENV": "local"}):
            with patch.object(
                orders.order_store,
                "create_order",
                side_effect=lambda order, *_: ("created", order),
            ) as create_order, patch.object(
                orders.order_store, "serialize_order", side_effect=lambda row: row
            ):
                status, _, body = orders.process_request(
                    "POST",
                    "/api/orders",
                    headers={"True-Client-IP": client_ip},
                    body_bytes=b'{"sku":"MRD-101","quantity":1}',
                )

        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["true_client_ip"], client_ip)
        self.assertEqual(create_order.call_args.args[0]["true_client_ip"], client_ip)

    def test_untrusted_certificate_headers_do_not_create_mtls_identity(self):
        forged_pem = (
            "-----BEGIN CERTIFICATE-----\n"
            "MIIB.fake-certificate-data\n"
            "-----END CERTIFICATE-----"
        )
        _, _, _, subject, verified, certificate, _ = storefront.extract_telemetry_headers({
            "Ssl-Client-Subject-Dn": "CN=forged",
            "Ssl-Client-Verify": "SUCCESS",
            "ssl-client-cert": forged_pem,
        })
        self.assertIsNone(subject)
        self.assertEqual(verified, "NOT_VERIFIED_BY_APPLICATION")
        self.assertIsNone(certificate)
        self.assertIsNone(orders.extract_telemetry_headers({
            "ssl-client-cert": forged_pem,
        })[3])

    def test_storefront_does_not_forward_client_certificate_headers(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"status":"ok"}'
        response.getcode.return_value = 201
        with patch.object(storefront.urllib.request, "urlopen", return_value=response) as urlopen:
            status, _, _ = storefront.process_request(
                "POST",
                "/api/checkout",
                headers={"ssl-client-cert": "forged-certificate-header"},
                body_bytes=b'{"sku":"MRD-101","quantity":1}',
            )

        self.assertEqual(status, 201)
        forwarded_headers = {
            name.lower() for name, _ in urlopen.call_args.args[0].header_items()
        }
        self.assertNotIn("ssl-client-cert", forwarded_headers)

    def test_reflected_proxy_headers_are_html_escaped(self):
        with patch.object(storefront.region_state, "is_drained", return_value=False):
            status, _, body = storefront.process_request(
                "GET", "/",
                headers={"True-Client-IP": "<script>alert(1)</script>"},
            )
        self.assertEqual(status, 200)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertNotIn(b"<script>alert(1)</script>", body)

    def test_order_console_renders_with_escaped_proxy_headers(self):
        with patch.object(orders.region_state, "is_drained", return_value=False):
            status, _, body = orders.process_request(
                "GET", "/",
                headers={"True-Client-IP": "<script>alert(1)</script>"},
            )
        self.assertEqual(status, 200)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertNotIn(b"<script>alert(1)</script>", body)


class SecretFileTests(unittest.TestCase):
    def test_secret_value_reads_file_and_removes_trailing_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "db-password"
            secret_path.write_text("local-pass\r\n", encoding="utf-8")
            with patch.dict(os.environ, {"DB_PASSWORD_FILE": str(secret_path)}, clear=True):
                self.assertEqual(get_secret_value("DB_PASSWORD"), "local-pass")


if __name__ == "__main__":
    unittest.main()
