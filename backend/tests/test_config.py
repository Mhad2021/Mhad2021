"""Configuration loading.

These exist because a config bug does not show up in any other test: every
other suite constructs settings in-process, while a real deployment loads them
from environment variables. A list field read from the environment was rejected
outright, so the server could not start with TRUSTED_HOSTS set — which is what
the deployment guide instructs.
"""
from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Write a .env and load Settings from it, the way a deployment does."""

    def _write(contents: str) -> Settings:
        path = tmp_path / ".env"
        path.write_text(contents)
        monkeypatch.chdir(tmp_path)
        return Settings(_env_file=str(path))

    return _write


class TestListSettings:
    def test_trusted_hosts_accepts_a_single_domain(self, env_file):
        settings = env_file("TRUSTED_HOSTS=presence.example.com\n")
        assert settings.trusted_hosts == ["presence.example.com"]

    def test_trusted_hosts_accepts_a_comma_separated_list(self, env_file):
        settings = env_file("TRUSTED_HOSTS=a.example.com,b.example.com\n")
        assert settings.trusted_hosts == ["a.example.com", "b.example.com"]

    def test_surrounding_whitespace_is_stripped(self, env_file):
        settings = env_file("TRUSTED_HOSTS= a.example.com , b.example.com \n")
        assert settings.trusted_hosts == ["a.example.com", "b.example.com"]

    def test_empty_entries_are_dropped(self, env_file):
        settings = env_file("CORS_ORIGINS=https://a.test,,https://b.test,\n")
        assert settings.cors_origins == ["https://a.test", "https://b.test"]

    def test_unset_falls_back_to_the_defaults(self, env_file):
        settings = env_file("ENVIRONMENT=development\n")
        assert settings.trusted_hosts == ["*"]
        assert settings.cors_origins == []

    def test_a_json_list_still_works(self, env_file):
        """Some deployment tools emit JSON; both forms must load."""
        settings = env_file('TRUSTED_HOSTS=["a.example.com","b.example.com"]\n')
        assert "a.example.com" in settings.trusted_hosts


class TestEnvExample:
    """The shipped .env.example must be loadable, or the deployment guide
    sends people into a crash on their first command."""

    def test_the_shipped_example_parses(self, tmp_path, monkeypatch):
        from pathlib import Path

        example = Path(__file__).resolve().parents[1] / ".env.example"
        assert example.exists(), ".env.example is missing"

        target = tmp_path / ".env"
        target.write_text(example.read_text())
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=str(target))
        assert settings.trusted_hosts
        assert settings.database_url


class TestDerivedValues:
    def test_is_production_tracks_the_environment(self, monkeypatch):
        # A real environment variable outranks .env, which is what a container
        # deployment relies on, so set it the same way here.
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert Settings().is_production
        monkeypatch.setenv("ENVIRONMENT", "development")
        assert not Settings().is_production

    def test_sqlite_is_accepted_for_evaluation(self, env_file):
        settings = env_file(
            "ENVIRONMENT=development\nDATABASE_URL=sqlite:///./presence-dev.db\n"
        )
        assert settings.database_url.startswith("sqlite")
