from click.testing import CliRunner
from tg_export.cli import main


def test_main_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output
    assert "account" in result.output
    assert "init" in result.output
    assert "run" in result.output
    assert "verify" in result.output


def test_auth_help():
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "credentials" in result.output
    assert "check" in result.output


def test_account_help():
    runner = CliRunner()
    result = runner.invoke(main, ["account", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "default" in result.output
    assert "remove" in result.output
