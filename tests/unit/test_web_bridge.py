import http.client
import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.board_control import (
    CEO_CHAT_COMMAND,
    SEND_CEO_MESSAGE,
    bind_board_principal,
)
from sourdough.company import FoundingTerms, found
from sourdough.ipc import ClientWebBridge, CompanyDaemon, IpcRefused
from sourdough.ipc.web_bridge import write_pending_intent
from sourdough.writer_lease import hold_company_writer


class ClientWebBridgeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-web-")))
        self.store = self.root / "c.sqlite"
        self.leases = self.root / "l"
        self.sock_dir = self.root / "s"
        self.pending_dir = self.root / "pending"
        self.static_dir = self.root / "dist"
        self.static_dir.mkdir()
        (self.static_dir / "index.html").write_text(
            "<!doctype html><html><body>Sourdough Client</body></html>",
            encoding="utf-8",
        )
        receipt = found(
            self.store, FoundingTerms("web-co", "charter-1", "paper", "profile-1")
        )
        self.company_id = receipt.company_id
        self.founder_id = receipt.founder_id
        with hold_company_writer(
            self.store, self.company_id, lease_directory=self.leases
        ) as writer:
            bind_board_principal(
                writer, principal_id="owner-1", role_name="board_control"
            )
            bind_board_principal(
                writer, principal_id="viewer-1", role_name="board_viewer"
            )

    def _raw_http(
        self,
        port: int,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            req_headers = {"Host": f"127.0.0.1:{port}"}
            if headers:
                req_headers.update(headers)
            conn.putrequest(method, path, skip_host=True)
            for key, value in req_headers.items():
                conn.putheader(key, value)
            if body or method in ("POST", "PUT"):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            data = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, data
        finally:
            conn.close()

    def test_rejects_non_loopback_host_binding(self):
        with self.assertRaisesRegex(IpcRefused, "127.0.0.1"):
            ClientWebBridge(self.root / "x.sock", host="0.0.0.0")

    def test_dns_rebinding_origin_query_token_and_session_cookie_guards(self):
        with (
            CompanyDaemon(
                self.store,
                self.company_id,
                socket_dir=self.sock_dir,
                lease_directory=self.leases,
            ) as daemon,
            ClientWebBridge(
                daemon.socket_path,
                static_dir=self.static_dir,
                pending_dir=self.pending_dir,
                principal="owner-1",
            ) as bridge,
        ):
            port = bridge.info.port

            # 1. DNS rebinding Host header rejected 403.
            st, _, _ = self._raw_http(
                port, "GET", "/api/v1/meta", headers={"Host": f"evil.local:{port}"}
            )
            self.assertEqual(st, 403)

            # 2. Cross-origin Origin rejected 403.
            st, _, _ = self._raw_http(
                port,
                "GET",
                "/api/v1/meta",
                headers={
                    "Origin": "http://attacker.example",
                    "Authorization": f"Bearer {bridge.info.session_token}",
                },
            )
            self.assertEqual(st, 403)

            # 3. Cross-site Sec-Fetch-Site rejected 403.
            st, _, _ = self._raw_http(
                port,
                "GET",
                "/api/v1/meta",
                headers={
                    "Sec-Fetch-Site": "cross-site",
                    "Authorization": f"Bearer {bridge.info.session_token}",
                },
            )
            self.assertEqual(st, 403)

            # 4. Query-string ?token= and ?TOKEN= rejected 400; Transfer-Encoding rejected 400.
            st, _, _ = self._raw_http(port, "GET", "/?token=secret")
            self.assertEqual(st, 400)
            st, _, _ = self._raw_http(port, "GET", "/?TOKEN=secret")
            self.assertEqual(st, 400)
            st, _, _ = self._raw_http(
                port,
                "POST",
                "/api/v1/commands",
                headers={"Transfer-Encoding": "chunked"},
            )
            self.assertEqual(st, 400)

            # 5. Unauthenticated /api/* rejected 401.
            st, _, _ = self._raw_http(port, "GET", "/api/v1/meta")
            self.assertEqual(st, 401)

            # 6. GET / serves SPA HTML with strict CSP nonce and HttpOnly SameSite=Strict cookie.
            st, hdrs, html = self._raw_http(port, "GET", "/")
            self.assertEqual(st, 200)
            self.assertIn(b"Sourdough Client", html)
            self.assertIn("default-src 'self'", hdrs["content-security-policy"])
            self.assertIn(
                "style-src-attr 'unsafe-inline'", hdrs["content-security-policy"]
            )
            cookie_hdr = hdrs["set-cookie"]
            self.assertIn(f"{bridge.info.cookie_name}=", cookie_hdr)
            self.assertIn("HttpOnly", cookie_hdr)
            self.assertIn("SameSite=Strict", cookie_hdr)
            cookie_pair = cookie_hdr.split(";", 1)[0]
            cookie_val = cookie_pair.split("=", 1)[1]

            # Cookie secret MUST NOT work as Authorization: Bearer (prevents cross-port escalation).
            st, _, _ = self._raw_http(
                port,
                "GET",
                "/api/v1/meta",
                headers={"Authorization": f"Bearer {cookie_val}"},
            )
            self.assertEqual(st, 401)

            # 7. Path traversal & NUL byte rejected.
            st, _, _ = self._raw_http(port, "GET", "/../../etc/passwd")
            self.assertEqual(st, 404)
            st, _, _ = self._raw_http(port, "GET", "/%00")
            self.assertEqual(st, 400)

            # 8. Authenticated /api/v1/meta works via session cookie.
            st, meta_hdrs, body = self._raw_http(
                port, "GET", "/api/v1/meta", headers={"Cookie": cookie_pair}
            )
            self.assertEqual(st, 200)
            self.assertEqual(meta_hdrs.get("cache-control"), "no-store")
            meta = json.loads(body.decode("utf-8"))
            self.assertEqual(meta["company_id"], self.company_id)

            # 9. Cookie-auth mutation WITHOUT matching Origin/Sec-Fetch-Site MUST fail closed 403.
            intent = {
                "command_id": "pend-1",
                "command_type": SEND_CEO_MESSAGE,
                "contract_version": CEO_CHAT_COMMAND,
                "preconditions": {
                    "ceo_agent_id": self.founder_id,
                    "charter_revision": "charter-1",
                },
                "payload": {"target_agent_id": self.founder_id, "body": "hi"},
            }
            st, _, _ = self._raw_http(
                port,
                "PUT",
                "/api/local/pending",
                headers={
                    "Cookie": cookie_pair,
                    "Content-Type": "application/json",
                },
                body=json.dumps(intent).encode("utf-8"),
            )
            self.assertEqual(st, 403)

            # With matching same-origin Origin header, /api/local/pending writes atomic 0o600 file.
            same_origin = {"Origin": f"http://127.0.0.1:{port}"}
            st, _, _ = self._raw_http(
                port,
                "PUT",
                "/api/local/pending",
                headers={
                    "Cookie": cookie_pair,
                    "Content-Type": "application/json",
                }
                | same_origin,
                body=json.dumps(intent).encode("utf-8"),
            )
            self.assertEqual(st, 200)
            stored_files = list(self.pending_dir.rglob("*.json"))
            self.assertEqual(len(stored_files), 1)
            self.assertEqual(stat.S_IMODE(os.lstat(stored_files[0]).st_mode), 0o600)

            st, _, pending_body = self._raw_http(
                port, "GET", "/api/local/pending", headers={"Cookie": cookie_pair}
            )
            self.assertEqual(st, 200)
            parsed = json.loads(pending_body.decode("utf-8"))
            self.assertEqual(parsed["pending"]["command_id"], "pend-1")
            self.assertEqual(parsed["receipt"]["status"], "not_found")

            # 10. Verify 403 stale-file quarantine when receipt belongs to another principal.
            st_cmd, _, _ = self._raw_http(
                port,
                "POST",
                "/api/v1/commands",
                headers={
                    "Cookie": cookie_pair,
                    "Content-Type": "application/json",
                    "Idempotency-Key": "owner-only-cmd",
                }
                | same_origin,
                body=json.dumps(
                    {
                        "company_id": self.company_id,
                        "command_id": "owner-only-cmd",
                        "command_type": SEND_CEO_MESSAGE,
                        "contract_version": CEO_CHAT_COMMAND,
                        "preconditions": {
                            "ceo_agent_id": self.founder_id,
                            "charter_revision": "charter-1",
                        },
                        "payload": {
                            "target_agent_id": self.founder_id,
                            "body": "owned",
                        },
                    }
                ).encode("utf-8"),
            )
            self.assertEqual(st_cmd, 200)

        # Now start a bridge as viewer-1 with a stale pending file pointing to owner-1's command_id.
        write_pending_intent(
            self.pending_dir,
            company_id=self.company_id,
            principal_id="viewer-1",
            intent={
                "company_id": self.company_id,
                "principal_id": "viewer-1",
                "command_id": "owner-only-cmd",
                "command_type": SEND_CEO_MESSAGE,
            },
        )
        with (
            CompanyDaemon(
                self.store,
                self.company_id,
                socket_dir=self.sock_dir,
                lease_directory=self.leases,
            ) as daemon,
            ClientWebBridge(
                daemon.socket_path,
                static_dir=self.static_dir,
                pending_dir=self.pending_dir,
                principal="viewer-1",
            ) as bridge,
        ):
            st, _, body = self._raw_http(
                bridge.info.port,
                "GET",
                "/api/local/pending",
                headers={"Authorization": f"Bearer {bridge.info.session_token}"},
            )
            self.assertEqual(st, 200)
            payload = json.loads(body.decode("utf-8"))
            self.assertTrue(payload["quarantined_stale"])
            self.assertIsNone(payload["pending"])
