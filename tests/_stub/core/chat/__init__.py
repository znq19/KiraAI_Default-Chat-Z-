"""core.chat package stub — the handful of names s/main.py imports at module level."""


class User:
    def __init__(self, user_id="", nickname=""):
        self.user_id = user_id
        self.nickname = nickname


class Group:
    def __init__(self, group_id="", group_name=""):
        self.group_id = group_id
        self.group_name = group_name


class Session:
    def __init__(self, adapter_name="qq", session_type="gm", session_id="10001",
                 session_title=""):
        self.adapter_name = adapter_name
        self.session_type = session_type
        self.session_id = session_id
        self.session_title = session_title

    @property
    def sid(self):
        return f"{self.adapter_name}:{self.session_type}:{self.session_id}"


class MessageChain:
    def __init__(self, elements=None):
        self.message_list = list(elements or [])

    def __iter__(self):
        return iter(self.message_list)

    def text(self, t):
        from core.chat.message_elements import Text
        self.message_list.append(Text(t))
        return self


class KiraIMMessage:
    def __init__(self, timestamp=0, sender=None, group=None, message_id="",
                 self_id="", chain=None, is_notice=False, is_mentioned=False,
                 raw_message=None):
        self.timestamp = timestamp
        self.sender = sender
        self.group = group
        self.message_id = message_id
        self.self_id = self_id
        self.chain = chain if isinstance(chain, MessageChain) else MessageChain(chain)
        self.is_notice = is_notice
        self.is_mentioned = is_mentioned
        self.raw_message = raw_message
        self.message_str = None
        self.extra = {}

    def is_group_message(self):
        return self.group is not None
