class Context:
    def __init__(self):
        self.sent = []
        self.web_routes = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))

    def register_web_api(self, route, handler, methods, desc=""):
        self.web_routes.append((route, handler, tuple(methods), desc))


class Star:
    def __init__(self, context=None):
        self.context = context
