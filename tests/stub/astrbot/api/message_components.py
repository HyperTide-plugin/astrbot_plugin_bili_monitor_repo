class _Component:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)

    def __repr__(self):
        payload = self.__dict__.get("args") or self.__dict__
        return f"{type(self).__name__}{payload}"


class Plain(_Component):
    pass


class At(_Component):
    pass


class Image(_Component):
    @staticmethod
    def fromURL(url):  # noqa: N802 - 对齐 AstrBot 的真实命名
        return Image(url=url)

    @staticmethod
    def from_url(url):
        return Image(url=url)
