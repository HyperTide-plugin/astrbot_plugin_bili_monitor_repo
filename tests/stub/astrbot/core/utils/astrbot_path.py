import os
import tempfile


def get_astrbot_data_path() -> str:
    path = os.path.join(tempfile.gettempdir(), "astrbot_stub_data")
    os.makedirs(path, exist_ok=True)
    return path
