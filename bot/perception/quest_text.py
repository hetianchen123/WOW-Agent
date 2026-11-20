"""Reassemble current quest context from the optional paged pixel strip."""
from dataclasses import dataclass

from . import channel_spec as pixels

ROWS = 12
PAGE_BYTES = 16
MAX_BYTES = 379


def quest_text_region(x, y):
    height = ROWS * pixels.REGION_H
    return x, y - 288 - height, pixels.REGION_W, height


@dataclass(frozen=True)
class QuestTextPage:
    sequence: int
    quest_id: int
    ordinal: int
    byte_count: int
    index: int
    checksum: int
    verified_mode: bool
    body: bytes


def decode_quest_text(image):
    if image.shape != (ROWS * pixels.REGION_H, pixels.REGION_W, 3):
        return None
    values = []
    for index in range(ROWS):
        frame = pixels.decode_frame(pixels.sample_nibbles(image, 0, index * pixels.REGION_H))
        if frame is None or frame[0] != index:
            return None
        values.append(frame[1])
    sequence, sync = divmod(values[0], 4096)
    meta = values[2]
    size, index, ordinal = meta % 512, (meta >> 9) % 32, (meta >> 14) % 32
    if (sync != 0xC71 or sequence > 127 or not values[1] or not ordinal or not 3 <= size <= MAX_BYTES
            or index * PAGE_BYTES >= size or any(value >> 16 != sequence % 16 for value in values[4:])):
        return None
    body = b"".join((value & 0xffff).to_bytes(2, "little") for value in values[4:])
    return QuestTextPage(sequence, values[1], ordinal, size, index, values[3], bool(meta >> 19), body)


@dataclass(frozen=True)
class QuestText:
    title: str
    native_type: str
    objective: str
    description: str


class QuestTextStream:
    def __init__(self):
        self.identity = None
        self.pages = {}
        self.text = None

    def observe(self, page, navigation):
        if (page is None or navigation is None or (page.sequence, page.quest_id, page.ordinal)
                != (navigation.sequence, navigation.quest_id, navigation.objective_index)):
            return
        identity = navigation.map_id, page.quest_id, page.ordinal, page.byte_count, page.checksum, page.verified_mode
        if self.identity != identity:
            self.identity = identity
            self.pages.clear()
            self.text = None
        if self.text is not None:
            return
        self.pages[page.index] = page.body
        count = (page.byte_count + PAGE_BYTES - 1) // PAGE_BYTES
        if len(self.pages) != count:
            return
        raw = b"".join(self.pages[index] for index in range(count))[:page.byte_count]
        checksum = 0
        for byte in raw:
            checksum = (checksum * 31 + byte) % 1048576
        if checksum != page.checksum:
            self.pages.clear()
            return
        try:
            self.text = QuestText(*raw.decode("utf-8").split("\x1f"))
        except (ValueError, TypeError):
            self.pages.clear()
