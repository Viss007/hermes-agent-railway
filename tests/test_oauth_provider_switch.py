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
        old_home = terminal.HERMES_HOME
        terminal.HERMES_HOME = home
        try:
            terminal._post_auth_heal_and_restart(provider_hint)
            return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        finally:
            terminal.HERMES_HOME = old_home


def main() -> None:
    selected = run_case("openai-codex", "openai")
    model = selected["model"]
    assert model["provider"] == "openai-codex", model
    assert model["base_url"] == "https://chatgpt.com/backend-api/codex", model
    assert model["default"] == "gpt-5.5", model
    assert selected["mcp_servers"]["cuebank"]["enabled"] is True, selected
    assert selected["skills"]["create_dir"] == "/data/skills-created", selected

    unchanged = run_case(None, "openai")
    assert unchanged["model"]["provider"] == "openai", unchanged
    assert unchanged["model"]["default"] == "gpt-4o", unchanged
    assert unchanged["mcp_servers"]["cuebank"]["enabled"] is True, unchanged
    print("ok: explicit Codex OAuth switches active provider; unrelated config is preserved")


if __name__ == "__main__":
    main()
