"""astrbot.api.web 的最小桩，用于本地校验仪表盘接口。"""


class _Query(dict):
    def get(self, key, default=None, type=None):  # noqa: A002 - 对齐真实签名
        value = dict.get(self, key, default)
        if value is None:
            return default
        if type is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value

    def getlist(self, key):
        value = dict.get(self, key)
        if isinstance(value, list):
            return value
        return [] if value is None else [value]


class _Request:
    def __init__(self):
        self.method = "GET"
        self.path = ""
        self.plugin_name = ""
        self.username = None
        self.headers = {}
        self.cookies = {}
        self.content_type = "application/json"
        self.client_host = "127.0.0.1"
        self.path_params = {}
        self.query = _Query()
        self.payload = {}

    async def json(self, default=None):
        return self.payload if self.payload else (default if default is not None else {})

    async def body(self):
        return b""

    async def form(self):
        return {}

    async def files(self):
        return {}


request = _Request()


def json_response(data, status_code=200):
    return {"status": "ok", "data": data}


def error_response(message, status_code=400):
    return ({"status": "error", "message": message}, status_code)


def file_response(path, filename="", content_type="application/octet-stream"):
    return {"status": "ok", "file": str(path), "filename": filename}


def stream_response(gen):
    return {"status": "ok", "stream": gen}
