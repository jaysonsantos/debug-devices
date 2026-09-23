import pytest

from debug_devices_mcp.h264 import (
    AccessUnitAssembler,
    AnnexBParser,
    codec_string,
    remove_emulation_prevention,
)

SPS = bytes.fromhex("67640020acb404a050f5")
PPS = bytes.fromhex("68ee06f2c0")
IDR = bytes.fromhex("65b841df0011")
P1 = bytes.fromhex("419a2233")
P2 = bytes.fromhex("419a4455")
# A second slice of the same picture: first_mb_in_slice is not 0 (the first bit is 0).
P2_SECOND_SLICE = bytes.fromhex("41226677")


def stream(*units: bytes, long_codes: bool = True) -> bytes:
    code = b"\x00\x00\x00\x01" if long_codes else b"\x00\x00\x01"
    return b"".join(code + unit for unit in units)


def assemble(data_chunks: list[bytes]) -> list:
    parser = AnnexBParser()
    assembler = AccessUnitAssembler()
    units = [unit for chunk in data_chunks for unit in parser.feed(chunk)] + parser.flush()
    access_units = [au for unit in units if (au := assembler.feed(unit)) is not None]
    if (last := assembler.flush()) is not None:
        access_units.append(last)
    return access_units


def test_codec_string_from_sps() -> None:
    assert codec_string(SPS) == "avc1.640020"
    assert codec_string(bytes.fromhex("6742c01e")) == "avc1.42c01e"


def test_codec_string_refuses_other_units() -> None:
    with pytest.raises(ValueError, match="SPS"):
        codec_string(PPS)


def test_emulation_prevention_is_removed() -> None:
    assert remove_emulation_prevention(bytes.fromhex("0000030100")) == bytes.fromhex("00000100")


@pytest.mark.parametrize("long_codes", [True, False])
def test_parser_splits_units_with_both_start_codes(long_codes: bool) -> None:
    parser = AnnexBParser()
    units = parser.feed(stream(SPS, PPS, IDR, long_codes=long_codes)) + parser.flush()
    assert units == [SPS, PPS, IDR]


def test_parser_handles_start_codes_across_chunks() -> None:
    data = stream(SPS, PPS, IDR, P1, P2)
    for size in (1, 2, 3, 5, 7):
        chunks = [data[index : index + size] for index in range(0, len(data), size)]
        parser = AnnexBParser()
        units = [unit for chunk in chunks for unit in parser.feed(chunk)] + parser.flush()
        assert units == [SPS, PPS, IDR, P1, P2], size


def test_parser_waits_for_the_next_start_code() -> None:
    parser = AnnexBParser()
    assert parser.feed(stream(SPS)) == []
    assert parser.feed(stream(PPS)) == [SPS]


def test_access_units_group_pictures_and_mark_key_frames() -> None:
    units = assemble([stream(SPS, PPS, IDR, P1, P2, P2_SECOND_SLICE)])
    assert [unit.key for unit in units] == [True, False, False]
    assert units[0].data == stream(SPS, PPS, IDR)
    assert units[2].data == stream(P2, P2_SECOND_SLICE)
    assert {unit.codec for unit in units} == {"avc1.640020"}


def test_later_key_frames_get_the_sps_and_pps() -> None:
    units = assemble([stream(SPS, PPS, IDR, P1, IDR, P2)])
    assert units[2].key
    assert units[2].data == stream(SPS, PPS, IDR)


def test_units_before_the_first_picture_make_no_access_unit() -> None:
    assert assemble([stream(SPS, PPS)]) == []
