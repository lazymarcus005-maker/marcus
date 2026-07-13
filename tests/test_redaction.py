from harness.runtime.redaction import redact_secrets


def test_redacts_entire_sensitive_file():
    content, count = redact_secrets(".env", "TOKEN=do-not-leak")

    assert content == "[REDACTED SENSITIVE FILE CONTENT]"
    assert count == 1


def test_redacts_inline_credentials_and_private_keys():
    source = "password = hunter2\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"

    content, count = redact_secrets("settings.txt", source)

    assert "hunter2" not in content
    assert "abc" not in content
    assert count == 2
