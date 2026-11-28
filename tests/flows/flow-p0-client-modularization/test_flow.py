"""Connected flow verification for flow-p0-client-modularization."""

from __future__ import annotations

import http.client
import json
import re
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.agent_runtime import PROVIDER_OPENAI, AgentProfileBinding
from sourdough.board_control import bind_board_principal
from sourdough.company import FoundingTerms, found
from sourdough.ipc import CompanyDaemon
from sourdough.writer_lease import hold_company_writer

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLIENT_SRC = _REPO_ROOT / "apps" / "client" / "src"
_HEX_FALLBACK_RE = re.compile(r"var\(--yg-[^,)]+,\s*(?:#[0-9a-fA-F]{3,8}|rgba?\()")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(socket_path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class ConnectedFlow(unittest.TestCase):
    def test_end_to_end(self) -> None:
        # 1. Verify modularized client architecture artifacts and Yogurt CSS hygiene.
        required_client_files = [
            "lib/format.ts",
            "lib/format.test.ts",
            "lib/statusTones.ts",
            "lib/domFocus.ts",
            "lib/routes.ts",
            "api/useProjection.ts",
            "api/useProjection.test.tsx",
            "api/types/index.ts",
            "state/PreferencesContext.tsx",
            "state/RouterContext.tsx",
            "state/ControlPlaneContext.tsx",
            "state/OutboxContext.tsx",
            "state/OutboxContext.test.tsx",
            "state/ApprovalsCacheContext.tsx",
            "components/ProjectionBoundary.tsx",
            "components/shell/AppShell.tsx",
            "components/shell/AppSidebar.tsx",
            "components/shell/AppTopbar.tsx",
            "components/chart/math.ts",
            "components/chart/math.test.ts",
            "features/agents/graph/layout.ts",
            "features/agents/graph/layout.test.ts",
            "features/agents/detail/AgentDetailView.tsx",
            "features/company/FinanceView.tsx",
            "features/system/SystemAuditView.tsx",
            "features/trading/desk/NewPaperBookCard.tsx",
            "features/trading/strategies/StrategiesView.tsx",
        ]
        for rel in required_client_files:
            path = _CLIENT_SRC / rel
            self.assertTrue(path.is_file(), f"Missing modularized client file: {rel}")

        self.assertTrue(
            (_REPO_ROOT / "apps" / "client" / "vitest.config.ts").is_file(),
            "Missing apps/client/vitest.config.ts",
        )
        for css_file in sorted((_CLIENT_SRC / "styles").glob("*.css")):
            text = css_file.read_text(encoding="utf-8")
            self.assertIsNone(
                _HEX_FALLBACK_RE.search(text),
                f"Literal color fallback prohibited in {css_file.name}",
            )

        # 2. Verify live CompanyDaemon ETag / If-None-Match / 304 Not Modified parity
        #    consumed by the client's shared useProjection<T> hook.
        with TemporaryDirectory(prefix="sd-c4-") as tmp:
            root = Path(tmp)
            co_dir = root / "co"
            co_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            store = co_dir / "c.sqlite"
            leases = root / "l"
            sock_dir = root / "s"
            profile = AgentProfileBinding(
                provider=PROVIDER_OPENAI,
                model="gpt-mock",
                revision="rev-1",
                credential_handle_ref="envref:OPENAI_API_KEY",
            )
            founding = found(
                store,
                FoundingTerms(
                    "ClientMod Co",
                    "charter-v1",
                    "manage-company",
                    profile.profile_id(),
                ),
            )
            with hold_company_writer(
                store, founding.company_id, lease_directory=leases
            ) as writer:
                bind_board_principal(
                    writer, principal_id="board-owner", role_name="board_control"
                )

            with CompanyDaemon(
                store,
                founding.company_id,
                socket_dir=sock_dir,
                lease_directory=leases,
                authorize_board=lambda _db, _cid, _op: "board-owner",
            ) as daemon:
                for endpoint in (
                    "/v1/projections/agents-graph",
                    "/v1/projections/approvals",
                    "/v1/projections/messages",
                    "/v1/projections/system-audit",
                ):
                    conn = _UnixHTTPConnection(daemon.socket_path)
                    try:
                        conn.request("GET", endpoint)
                        first = conn.getresponse()
                        body = first.read()
                        self.assertEqual(
                            first.status,
                            200,
                            f"Expected 200 on initial GET {endpoint}",
                        )
                        etag = first.getheader("ETag")
                        self.assertTrue(
                            etag, f"Expected non-empty ETag header for {endpoint}"
                        )
                        payload = json.loads(body.decode("utf-8"))
                        self.assertEqual(payload["company_id"], founding.company_id)
                    finally:
                        conn.close()

                    conn2 = _UnixHTTPConnection(daemon.socket_path)
                    try:
                        conn2.request("GET", endpoint, headers={"If-None-Match": etag})
                        second = conn2.getresponse()
                        second.read()
                        self.assertEqual(
                            second.status,
                            304,
                            f"Expected 304 Not Modified on conditional GET {endpoint}",
                        )
                    finally:
                        conn2.close()


if __name__ == "__main__":
    unittest.main()
