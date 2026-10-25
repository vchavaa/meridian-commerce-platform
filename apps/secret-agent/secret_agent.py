import os
import secrets
import signal
import time
from pathlib import Path


SECRETS_DIRECTORY = Path(os.environ.get("SECRETS_DIRECTORY", "/run/meridian-secrets"))


def write_secret(value, filename):
    if not value:
        raise RuntimeError(f"Required local secret for {filename} was not provided")

    SECRETS_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(SECRETS_DIRECTORY, 0o755)
    destination = SECRETS_DIRECTORY / filename
    temporary = SECRETS_DIRECTORY / f".{filename}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(value)
            secret_file.flush()
            os.fsync(secret_file.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o444)
    finally:
        if temporary.exists():
            temporary.unlink()


def stop_agent(_signum, _frame):
    admin_token = SECRETS_DIRECTORY / "local_admin_token"
    if admin_token.exists():
        admin_token.unlink()
    raise SystemExit(0)


def main():
    database_password = SECRETS_DIRECTORY / "postgres_password"
    if not database_password.exists():
        write_secret(secrets.token_urlsafe(32), "postgres_password")

    write_secret(os.environ.get("LOCAL_ADMIN_TOKEN", ""), "local_admin_token")
    signal.signal(signal.SIGTERM, stop_agent)
    signal.signal(signal.SIGINT, stop_agent)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
