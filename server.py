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
AGENTS_FILE = ROOT / "agents.json"


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
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_COOKIE_NAME = "chatkit_user_id"


def resolve_env_reference(value):
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if stripped.startswith("${") and stripped.endswith("}"):
        return os.environ.get(stripped[2:-1], "")
    return stripped


def load_agent_registry():
    if not AGENTS_FILE.exists():
        return []

    raw_agents = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(raw_agents, list):
        return []

    agents = []
    for raw_agent in raw_agents:
        if not isinstance(raw_agent, dict):
            continue

        agent = dict(raw_agent)
        agent["id"] = str(agent.get("id", "")).strip()
        agent["name"] = str(agent.get("name", "")).strip()
        agent["enabled"] = bool(agent.get("enabled", False))
        agent["featured"] = bool(agent.get("featured", False))
        agent["order"] = int(agent.get("order", 999))
        agent["categories"] = list(agent.get("categories", []))
        agent["tags"] = list(agent.get("tags", []))
        agent["audiences"] = list(agent.get("audiences", []))
        agent["status"] = str(agent.get("status", "inactive")).strip() or "inactive"
        agent["workflow_id"] = resolve_agent_workflow_id(agent)
        agent["workflow_version"] = resolve_agent_workflow_version(agent)
        agent["ready"] = bool(OPENAI_API_KEY and agent["workflow_id"] and agent["enabled"])
        agents.append(agent)

    agents.sort(key=lambda item: (item["order"], item["name"]))
    return agents


def resolve_agent_workflow_id(agent):
    direct_value = resolve_env_reference(agent.get("workflow_id", ""))
    if direct_value:
        return direct_value

    workflow_env = str(agent.get("workflow_env", "")).strip()
    if workflow_env and os.environ.get(workflow_env):
        return os.environ[workflow_env]

    fallback_env = str(agent.get("workflow_fallback_env", "OPENAI_WORKFLOW_ID")).strip()
    if fallback_env and os.environ.get(fallback_env):
        return os.environ[fallback_env]

    return ""


def resolve_agent_workflow_version(agent):
    direct_value = resolve_env_reference(agent.get("workflow_version", ""))
    if direct_value:
        return direct_value

    version_env = str(agent.get("workflow_version_env", "")).strip()
    if version_env and os.environ.get(version_env):
        return os.environ[version_env]

    fallback_env = str(agent.get("workflow_version_fallback_env", "OPENAI_WORKFLOW_VERSION")).strip()
    if fallback_env and os.environ.get(fallback_env):
        return os.environ[fallback_env]

    return ""


def serialize_agent(agent):
    fields = [
        "id",
        "name",
        "subtitle",
        "description",
        "status",
        "enabled",
        "featured",
        "categories",
        "tags",
        "audiences",
        "accent",
        "icon",
        "order",
        "welcome_title",
        "welcome_text",
        "placeholder",
        "empty_state_hint",
        "ready",
    ]
    return {field: agent.get(field) for field in fields}


def find_default_agent(agents):
    ready_agents = [agent for agent in agents if agent.get("ready")]
    featured_ready_agents = [agent for agent in ready_agents if agent.get("featured")]
    if featured_ready_agents:
        return featured_ready_agents[0]
    if ready_agents:
        return ready_agents[0]
    if agents:
        return agents[0]
    return None


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

        if self.path == "/api/agents":
            self._send_agents()
            return

        super().do_GET()

    def _send_agents(self):
        agents = load_agent_registry()
        default_agent = find_default_agent(agents)
        payload = {
            "agents": [serialize_agent(agent) for agent in agents if agent.get("enabled")],
            "default_agent_id": default_agent.get("id") if default_agent else None,
        }
        self._send_json(200, payload)

    def _create_chatkit_session(self):
        if not OPENAI_API_KEY:
            self._send_json(500, {"error": "Missing OPENAI_API_KEY. Add it to the environment."})
            return

        body = self._read_json_body()
        agent_id = str(body.get("agent_id", "")).strip()
        agents = load_agent_registry()
        default_agent = find_default_agent(agents)
        selected_agent = next((agent for agent in agents if agent.get("id") == agent_id), default_agent)

        if not selected_agent:
            self._send_json(404, {"error": "No configured agents were found."})
            return

        if not selected_agent.get("ready"):
            self._send_json(
                400,
                {
                    "error": f"Agent '{selected_agent.get('name')}' is not ready. Add a workflow id for it."
                },
            )
            return

        payload = {
            "workflow": {"id": selected_agent["workflow_id"]},
            "user": self._get_or_create_user_id(),
        }
        if selected_agent.get("workflow_version"):
            payload["workflow"]["version"] = selected_agent["workflow_version"]

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
            print(f"ChatKit session created successfully for {selected_agent['id']}.")
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
            {
                "client_secret": body.get("client_secret", ""),
                "agent_id": selected_agent["id"],
            },
            headers=self._session_cookie_headers(),
        )

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            return {}

        raw_body = self.rfile.read(content_length).decode("utf-8")
        if not raw_body.strip():
            return {}

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return {}

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
