"""Stub of core.chat.message_elements — rich enough for the media module tests."""


class Text:
    def __init__(self, text=""):
        self.text = text

    @property
    def repr(self):
        return self.text


class At:
    def __init__(self, pid=""):
        self.pid = pid
        self.nickname = ""


class Reply:
    def __init__(self, chain=None):
        self.chain = chain or []


class Poke:
    pass


class Image:
    def __init__(self, image=None, caption=None):
        self.image = image
        self.caption = caption
        self.md5 = None
        self._file = None

    async def hash_image(self):
        return "md5image0000"

    async def to_path(self):
        return self.image

    async def to_base64(self):
        return ""


class Sticker(Image):
    def __init__(self, sticker_id="s1", sticker=None, caption=None):
        super().__init__(image=sticker, caption=caption)
        self.sticker_id = sticker_id

    async def hash_image(self):
        return "md5sticker00"


class Record:
    def __init__(self, record=None, caption=None):
        self.record = record
        self.caption = caption


class File:
    def __init__(self, name=""):
        self.name = name


class Video(File):
    pass


class Forward:
    def __init__(self, chains=None):
        self.chains = chains or []
