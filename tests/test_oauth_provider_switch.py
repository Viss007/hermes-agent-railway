"""Regression checks for explicit subscription OAuth provider selection."""

import json
import tempfile
from pathlib import Path

import yaml

from admin import terminal


def run_case(provider_hint: str | None, current_provider: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        auth = {
            "credential_pool": {
                "openai-codex": [{"id": "test-credential-only"}]
            }
        }
        (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
        config = {
            "model": {
                "provider": current_provider,
                "base_url": "https://old-api.example/v1",
                "default": "gpt-4o",
            },
            "mcp_servers": {
                "cuebank": {"url": "https://memory.example/mcp", "enabled": True}
            },
            "skills": {"create_dir": "/data/skills-created"},
        }
        (home / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        session_dir = home / ".hermes" / "webui" / "sessions"
        session_dir.mkdir(parents=True)
        session = {
            "model": "deepseek-4.1-uncensored",
            "model_provider": current_provider,
            "messages": [{"role": "user", "content": "keep this history"}],
        }
        (session_dir / "old-session.json").write_text(
            json.dumps(session), encoding="utf-8"
        )
        index = [{"session_id": "old-session", **session}]
        (session_dir / "_index.json").write_text(
            json.dumps(index), encoding="utf-8"
        )

        old_home = terminal.HERMES_HOME
        terminal.HERMES_HOME = home
        try:
            terminal._post_auth_heal_and_restart(provider_hint)
            result = {
                "config": yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")),
                "session": json.loads((session_dir / "old-session.json").read_text(encoding="utf-8")),
                "index": json.loads((session_dir / "_index.json").read_text(encoding="utf-8")),
            }
            return result
        finally:
            terminal.HERMES_HOME = old_home


def main() -> None:
    selected = run_case("openai-codex", "custom")
    config = selected["config"]
    assert config["model"]["provider"] == "openai-codex", config
    assert config["model"]["base_url"] == "https://chatgpt.com/backend-api/codex", config
    assert config["model"]["default"] == "gpt-5.5", config
    assert config["mcp_servers"]["cuebank"]["enabled"] is True, config
    assert config["skills"]["create_dir"] == "/data/skills-created", config
    assert selected["session"]["model_provider"] == "openai-codex", selected
    assert selected["session"]["model"] == "gpt-5.5", selected
    assert selected["session"]["messages"][0]["content"] == "keep this history", selected
    assert selected["index"][0]["model_provider"] == "openai-codex", selected
    assert selected["index"][0]["model"] == "gpt-5.5", selected

    unchanged = run_case(None, "custom")
    assert unchanged["config"]["model"]["provider"] == "custom", unchanged
    assert unchanged["config"]["model"]["default"] == "gpt-4o", unchanged
    assert unchanged["session"]["model_provider"] == "custom", unchanged
    assert unchanged["session"]["model"] == "deepseek-4.1-uncensored", unchanged
    assert unchanged["config"]["mcp_servers"]["cuebank"]["enabled"] is True, unchanged
    print("ok: explicit Codex OAuth switches config and existing sessions; history and MCP config are preserved")


if __name__ == "__main__":
    main()
