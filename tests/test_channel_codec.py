import numpy as np
import pytest

from bot.perception import channel_spec as spec


def test_roundtrip_all_types():
    cases = [
        (spec.MSG_POS, spec.pack_pos(0.123456, 0.654321)),
        (spec.MSG_HDG, spec.pack_hdg(2.9345)),
        (spec.MSG_STATUS, spec.pack_status(spec.StatusFlags.IN_COMBAT | spec.StatusFlags.MOUNTED, 73)),
        (spec.MSG_BAGSUM, spec.pack_bagsum(42, 1234)),
        (spec.MSG_FASTEV, spec.pack_fastev(spec.FastEvKind.CAST_START, 17, 1)),
        (spec.MSG_GOLD, 999_999),
        (spec.MSG_ZONE, 2022),
        (spec.MSG_BAG, (5 << 16) | 777),
    ]
    for mtype, value in cases:
        nibbles = spec.encode_frame(mtype, value)
        got = spec.decode_frame(nibbles)
        assert got == (mtype, value)


def test_decode_rejects_corruption():
    nibbles = spec.encode_frame(spec.MSG_POS, spec.pack_pos(0.5, 0.5))
    bad = list(nibbles)
    bad[3] = (bad[3] + 1) % 16  # flip a data nibble -> checksum mismatch
    assert spec.decode_frame(bad) is None
    bad2 = list(nibbles)
    bad2[0] = 0x5  # wrong sync
    assert spec.decode_frame(bad2) is None
    assert spec.decode_frame(nibbles[:6]) is None


def test_render_and_sample_exact():
    rng = np.random.default_rng(7)
    canvas = rng.integers(0, 255, size=(300, 500, 3), dtype=np.uint8)
    ox, oy = 137, 211
    nibbles = spec.encode_frame(spec.MSG_HDG, spec.pack_hdg(1.2345))
    spec.render_frame(canvas, ox, oy, nibbles)
    assert spec.sample_nibbles(canvas, ox, oy) == nibbles


def test_render_and_sample_with_noise_tolerance():
    canvas = np.zeros((300, 500, 3), dtype=np.uint8)
    ox, oy = 20, 240
    nibbles = spec.encode_frame(spec.MSG_STATUS, spec.pack_status(0, 50))
    spec.render_frame(canvas, ox, oy, nibbles)
    noise = np.random.default_rng(3).integers(-25, 26, size=canvas.shape)
    canvas = np.clip(canvas.astype(int) + noise, 0, 255).astype(np.uint8)
    assert spec.sample_nibbles(canvas, ox, oy) == nibbles


def test_pos_hdg_precision():
    x, y = 0.987, 0.013
    rx, ry = spec.unpack_pos(spec.pack_pos(x, y))
    assert abs(rx - x) < 1 / 1023 * 2
    assert abs(ry - y) < 1 / 1023 * 2
    h = 5.9
    assert abs(spec.unpack_hdg(spec.pack_hdg(h)) - h) < 1e-5


def test_status_flags_roundtrip():
    flags = spec.StatusFlags.IN_COMBAT | spec.StatusFlags.LOOT_OPEN | spec.StatusFlags.MOVING
    v = spec.pack_status(flags, 42)
    out_flags, bucket = spec.unpack_status(v)
    assert out_flags & spec.StatusFlags.IN_COMBAT
    assert out_flags & spec.StatusFlags.LOOT_OPEN
    assert out_flags & spec.StatusFlags.MOVING
    assert not (out_flags & spec.StatusFlags.DEAD)
    assert 0 <= bucket <= 15
    unknown = flags | spec.StatusFlags.HP_UNKNOWN
    assert spec.unpack_status(spec.pack_status(unknown, 0)) == (unknown, 0)


def test_schedule_covers_all_kinds():
    kinds = set()
    slowrot = set()
    for tick in range(spec.SLOWROT_LEN * 20 + 40):  # enough cycles to cover rotation
        k = spec.schedule_kind(tick)
        if k is None:
            slowrot.add(spec.slowrot_index(tick))
        else:
            kinds.add(k)
    assert spec.MSG_POS in kinds and spec.MSG_HDG in kinds
    assert spec.MSG_STATUS in kinds and spec.MSG_BAGSUM in kinds
    assert spec.MSG_FASTEV in kinds
    assert slowrot == set(range(spec.SLOWROT_LEN))


def test_channel_reader_roundtrip():
    from bot.core.config import ChannelConfig
    from bot.perception.channel import ChannelReader

    cfg = ChannelConfig(x=40, y=320)
    reader = ChannelReader(cfg)
    canvas = np.zeros((360, 640, 3), dtype=np.uint8)

    def feed(mtype, value):
        spec.render_frame(canvas, 40, 320, spec.encode_frame(mtype, value))
        # reader expects the cropped 142x16 strip
        reader.update(canvas[320 : 320 + spec.REGION_H, 40 : 40 + spec.REGION_W].copy())

    feed(spec.MSG_POS, spec.pack_pos(0.5, 0.5))
    feed(spec.MSG_HDG, spec.pack_hdg(1.0))
    feed(spec.MSG_BAGSUM, spec.pack_bagsum(20, 5))
    feed(spec.MSG_FASTEV, spec.pack_fastev(1, 1, 1))
    assert reader.pos() == pytest.approx((0.5, 0.5), abs=2 / 1023)
    assert reader.heading() == pytest.approx(1.0, abs=1e-5)
    assert reader.bagsum() == (20, 5)
    assert reader.is_alive()
    assert len(reader.events) == 1 and reader.events[0].kind == 1
    assert reader.frames_ok == 4 and reader.frames_bad == 0
