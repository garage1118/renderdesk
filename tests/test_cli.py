from click.testing import CliRunner

from renderdesk.cli import create_user


def test_create_user_rejects_short_password():
    runner = CliRunner()
    result = runner.invoke(create_user, ["--email", "short@example.com"], input="short\nshort\n")
    assert result.exit_code != 0
    assert "at least 8 characters" in result.output


def test_create_user_rejects_malformed_email():
    runner = CliRunner()
    result = runner.invoke(create_user, ["--email", "not-an-email"], input="longenoughpw\nlongenoughpw\n")
    assert result.exit_code != 0
    assert "email" in result.output.lower()
