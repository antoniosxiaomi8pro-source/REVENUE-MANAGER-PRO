import json
import os
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import requests


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
ENV_FILE = ROOT / ".env"


def load_env_file():
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), cleaned)


load_env_file()


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_WORKFLOW_ID = os.environ.get("OPENAI_WORKFLOW_ID", "")
OPENAI_WORKFLOW_VERSION = os.environ.get("OPENAI_WORKFLOW_VERSION", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_COOKIE_NAME = "chatkit_user_id"


class ChatKitHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def do_POST(self):
        if self.path == "/api/chatkit/session":
            self._create_chatkit_session()
            return

        self.send_error(404, "Not found")

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        super().do_GET()

    def _create_chatkit_session(self):
        if not OPENAI_API_KEY:
            self._send_json(
                500,
                {
                    "error": "Missing OPENAI_API_KEY. Add it to the .env file."
                },
            )
            return

        if not OPENAI_WORKFLOW_ID:
            self._send_json(
                500,
                {
                    "error": "Missing OPENAI_WORKFLOW_ID. Add it to the .env file."
                },
            )
            return

        payload = {
            "workflow": {"id": OPENAI_WORKFLOW_ID},
            "user": self._get_or_create_user_id(),
        }
        if OPENAI_WORKFLOW_VERSION:
            payload["workflow"]["version"] = OPENAI_WORKFLOW_VERSION

        try:
            response = requests.post(
                "https://api.openai.com/v1/chatkit/sessions",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "chatkit_beta=v1",
                },
                timeout=30,
            )
            body = response.json()
            if not response.ok:
                print("ChatKit session error:", body)
                self._send_json(response.status_code, body)
                return
            print("ChatKit session created successfully.")
        except requests.RequestException as exc:
            print("Network error while creating ChatKit session:", str(exc))
            self._send_json(500, {"error": str(exc)})
            return
        except ValueError:
            error_body = response.text if "response" in locals() else ""
            try:
                parsed = json.loads(error_body)
            except json.JSONDecodeError:
                parsed = {"error": error_body or "OpenAI request failed"}
            print("Unexpected non-JSON response:", parsed)
            self._send_json(500, parsed)
            return

        self._send_json(
            200,
            {"client_secret": body.get("client_secret", "")},
            headers=self._session_cookie_headers(),
        )

    def _get_or_create_user_id(self):
        cookie_header = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        if cookie_header:
            jar.load(cookie_header)

        existing = jar.get(SESSION_COOKIE_NAME)
        if existing and existing.value:
            self._pending_cookie_user_id = existing.value
            return existing.value

        generated = f"web-{uuid4().hex}"
        self._pending_cookie_user_id = generated
        return generated

    def _session_cookie_headers(self):
        user_id = getattr(self, "_pending_cookie_user_id", "")
        if not user_id:
            return []

        cookie = cookies.SimpleCookie()
        cookie[SESSION_COOKIE_NAME] = user_id
        cookie[SESSION_COOKIE_NAME]["path"] = "/"
        cookie[SESSION_COOKIE_NAME]["httponly"] = True
        cookie[SESSION_COOKIE_NAME]["samesite"] = "Lax"
        return [("Set-Cookie", cookie.output(header="").strip())]

    def _send_json(self, status_code, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ChatKitHandler)
    print(f"Open http://127.0.0.1:{PORT}")
    server.serve_forever()
