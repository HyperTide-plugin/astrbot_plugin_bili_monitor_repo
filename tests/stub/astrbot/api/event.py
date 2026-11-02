from dataclasses import dataclass


class MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def __repr__(self):
        return f"MessageChain({self.chain!r})"


class _Filter:
    def command(self, name, **kwargs):
        def deco(fn):
            fn.__command_name__ = name
            return fn

        return deco

    def __getattr__(self, item):
        def deco(*args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return lambda fn: fn

        return deco


filter = _Filter()


@dataclass
class AstrMessageEvent:
    message_str: str = ""
    unified_msg_origin: str = ""
    admin: bool = True

    def is_admin(self) -> bool:
        return self.admin

    def plain_result(self, text):
        return MessageChain([text])
