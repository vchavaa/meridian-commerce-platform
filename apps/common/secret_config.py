import os


def get_secret_value(name, default=""):
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            with open(file_path, encoding="utf-8") as secret_file:
                return secret_file.read().rstrip("\r\n")
        except OSError as exc:
            raise RuntimeError(f"Unable to read configured secret file for {name}") from exc
    return os.environ.get(name, default)
