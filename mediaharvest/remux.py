"""纯 Python 的 MPEG-TS -> MP4 (ISO BMFF) 重封装器，仅支持 H.264 + AAC。

本模块**不依赖任何第三方库**，也**不调用 ffmpeg**，只使用标准库。
它把 HLS 下载得到的 ``.ts`` 文件重新封装成可以直接播放的 ``.mp4``：

    >>> from mediaharvest.remux import ts_to_mp4
    >>> ts_to_mp4("/tmp/a.ts", "/tmp/a.mp4")
    True

公共 API::

    ts_to_mp4(ts_path, mp4_path, *, faststart=True) -> bool
    probe_ts(ts_path) -> Dict[str, Any]
    RemuxError

设计要点：

* TS 层：按 188 字节包解析，自动重同步（文件头可能有偏移），
  解析 PAT -> PMT -> 基本流，组装 PES，解析 33 位 PTS/DTS。
* H.264：Annex-B 起始码切分 NAL，SPS/PPS 放进 ``avcC``，
  每个样本转成 AVCC（4 字节大端长度前缀），IDR 标记为同步样本。
* AAC：解析 ADTS 头，去掉头部得到裸 AAC，构造 ``esds``。
* MP4：写出 ``ftyp`` / ``moov`` / ``mdat``；音视频按约 0.5 秒的
  chunk 交叉交织；``faststart`` 时 ``moov`` 位于 ``mdat`` 之前。
* 时间轴：两条轨道归一到共同的起始时刻，落后的轨道用空的
  ``elst``（edit list）表达起始延迟，保证音画同步。

模块导入本身没有副作用，且不会向 stdout 打印任何内容。
"""

from __future__ import annotations

import os
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["ts_to_mp4", "probe_ts", "RemuxError"]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

#: 视频时间基（PES 的 90 kHz 时钟）
VIDEO_TIMESCALE = 90000

#: movie header / mdhd 使用的时间基
MOVIE_TIMESCALE = 1000

#: PMT stream_type
ST_MPEG1_VIDEO = 0x01
ST_MPEG2_VIDEO = 0x02
ST_MPEG1_AUDIO = 0x03
ST_MPEG2_AUDIO = 0x04
ST_PRIVATE = 0x06
ST_AAC_ADTS = 0x0F
ST_AAC_LATM = 0x11
ST_H264 = 0x1B
ST_HEVC = 0x24

#: 一个 chunk 的目标时长（秒），用于交织
CHUNK_SECONDS = 0.5

#: 相邻样本时间戳超过该值（秒）视为异常，用中位数兜底
MAX_SANE_DELTA = 10.0

#: 兜底帧时长（秒）
DEFAULT_FRAME_DURATION = 1.0 / 30.0

#: 每次从磁盘读取的块大小
READ_CHUNK = 4 * 1024 * 1024

#: 单轨样本数上限（防止异常输入耗尽内存）
MAX_SAMPLES_PER_TRACK = 4_000_000

#: AAC 采样率表
AAC_SAMPLE_RATES = [
    96000, 88200, 64000, 48000, 44100, 32000,
    24000, 22050, 16000, 12000, 11025, 8000, 7350,
]

#: ISO-639-2/T "und"（未定义语言），打包成 mdhd 的 5 位一组
UND_LANGUAGE = ((ord("u") - 0x60) << 10) | ((ord("n") - 0x60) << 5) | (ord("d") - 0x60)


class RemuxError(Exception):
    """重封装过程中出现的、无法恢复的错误。"""


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------


def _u16(data: bytes, off: int = 0) -> int:
    return (data[off] << 8) | data[off + 1]


def _median(values: Sequence[int]) -> float:
    """返回中位数（不修改入参）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _hex_byte(value: int) -> str:
    return "%02x" % (value & 0xFF)


# --------------------------------------------------------------------------
# 位流读取（Exp-Golomb）+ H.264 RBSP 去防竞争字节
# --------------------------------------------------------------------------


def _unescape_rbsp(data: bytes) -> bytes:
    """移除 H.264 的 ``00 00 03`` 防竞争字节。"""
    if b"\x00\x00\x03" not in data:
        return data
    out = bytearray(len(data))
    written = 0
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out[written] = byte
        written += 1
        zeros = zeros + 1 if byte == 0x00 else 0
    del out[written:]
    return bytes(out)


class _BitReader(object):
    """大端位流读取器。"""

    __slots__ = ("_data", "_pos", "_size")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self._size = len(data) * 8

    def bits_left(self) -> int:
        return self._size - self._pos

    def u(self, count: int) -> int:
        if count <= 0:
            return 0
        if self._pos + count > self._size:
            raise RemuxError("位流越界")
        value = 0
        pos = self._pos
        data = self._data
        remaining = count
        while remaining > 0:
            byte_index = pos >> 3
            bit_offset = pos & 7
            avail = 8 - bit_offset
            take = avail if avail < remaining else remaining
            chunk = (data[byte_index] >> (avail - take)) & ((1 << take) - 1)
            value = (value << take) | chunk
            pos += take
            remaining -= take
        self._pos = pos
        return value

    def flag(self) -> bool:
        return self.u(1) == 1

    def ue(self) -> int:
        leading = 0
        while True:
            if self._pos >= self._size:
                raise RemuxError("ue(v) 越界")
            if self.u(1):
                break
            leading += 1
            if leading > 32:
                raise RemuxError("ue(v) 前缀过长")
        if leading == 0:
            return 0
        return (1 << leading) - 1 + self.u(leading)

    def se(self) -> int:
        code = self.ue()
        if code & 1:
            return (code + 1) // 2
        return -(code // 2)


# --------------------------------------------------------------------------
# SPS 解析
# --------------------------------------------------------------------------

#: 这些 profile_idc 的 SPS 里带 chroma_format_idc / scaling matrix
_HIGH_PROFILES = frozenset((100, 110, 122, 244, 44, 83, 86))


def _skip_scaling_list(br: _BitReader, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            next_scale = (last_scale + br.se() + 256) % 256
        last_scale = next_scale if next_scale != 0 else last_scale


def _skip_scaling_matrix(br: _BitReader, count: int) -> None:
    for index in range(count):
        if br.flag():
            _skip_scaling_list(br, 16 if index < 6 else 64)


def _parse_sps(nal: bytes) -> Dict[str, Any]:
    """解析 SPS NAL（含 1 字节 NAL header），返回分辨率等信息。"""
    if len(nal) < 5:
        raise RemuxError("SPS 长度过短")
    br = _BitReader(_unescape_rbsp(nal[1:]))

    profile_idc = br.u(8)
    constraint_flags = br.u(8)
    level_idc = br.u(8)
    sps_id = br.ue()

    chroma_format_idc = 1
    separate_colour_plane_flag = 0
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = br.ue()
        if chroma_format_idc == 3:
            separate_colour_plane_flag = br.u(1)
        br.ue()  # bit_depth_luma_minus8
        br.ue()  # bit_depth_chroma_minus8
        br.u(1)  # qpprime_y_zero_transform_bypass_flag
        if br.flag():  # seq_scaling_matrix_present_flag
            _skip_scaling_matrix(br, 8 if chroma_format_idc != 3 else 12)

    br.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = br.ue()
    if pic_order_cnt_type == 0:
        br.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif pic_order_cnt_type == 1:
        br.u(1)  # delta_pic_order_always_zero_flag
        br.se()  # offset_for_non_ref_pic
        br.se()  # offset_for_top_to_bottom_field
        cycle = br.ue()
        if cycle > 256:
            raise RemuxError("SPS: num_ref_frames_in_pic_order_cnt_cycle 异常")
        for _ in range(cycle):
            br.se()

    br.ue()  # max_num_ref_frames
    br.u(1)  # gaps_in_frame_num_value_allowed_flag

    pic_width_in_mbs_minus1 = br.ue()
    pic_height_in_map_units_minus1 = br.ue()
    frame_mbs_only_flag = br.u(1)
    if not frame_mbs_only_flag:
        br.u(1)  # mb_adaptive_frame_field_flag
    br.u(1)  # direct_8x8_inference_flag

    crop_left = crop_right = crop_top = crop_bottom = 0
    if br.flag():  # frame_cropping_flag
        crop_left = br.ue()
        crop_right = br.ue()
        crop_top = br.ue()
        crop_bottom = br.ue()

    width = (pic_width_in_mbs_minus1 + 1) * 16
    height = (pic_height_in_map_units_minus1 + 1) * 16 * (1 if frame_mbs_only_flag else 2)

    if chroma_format_idc == 0 or separate_colour_plane_flag:
        crop_unit_x = 1
        crop_unit_y = 2 - frame_mbs_only_flag
    else:
        sub_width_c = 2 if chroma_format_idc == 3 else 1
        sub_height_c = 2 if chroma_format_idc == 1 else 1
        crop_unit_x = sub_width_c
        crop_unit_y = sub_height_c * (2 - frame_mbs_only_flag)

    width -= (crop_left + crop_right) * crop_unit_x
    height -= (crop_top + crop_bottom) * crop_unit_y
    if width <= 0 or height <= 0:
        raise RemuxError("SPS 计算出的分辨率非法")

    return {
        "profile_idc": profile_idc,
        "constraint_flags": constraint_flags,
        "level_idc": level_idc,
        "sps_id": sps_id,
        "width": width,
        "height": height,
        "chroma_format_idc": chroma_format_idc,
    }


def _codec_string(info: Dict[str, Any]) -> str:
    return "avc1.%s%s%s" % (
        _hex_byte(info["profile_idc"]),
        _hex_byte(info["constraint_flags"]),
        _hex_byte(info["level_idc"]),
    )


# --------------------------------------------------------------------------
# Annex-B 处理
# --------------------------------------------------------------------------


def _split_annex_b(data: bytes) -> List[bytes]:
    """把 Annex-B 码流切分成 NAL 单元（不含起始码）。"""
    nals: List[bytes] = []
    size = len(data)
    if size < 4:
        return nals
    start = data.find(b"\x00\x00\x01")
    if start < 0:
        return nals
    pos = start + 3
    while pos < size:
        nxt = data.find(b"\x00\x00\x01", pos)
        if nxt < 0:
            end = size
        else:
            end = nxt
            if end > pos and data[end - 1] == 0x00:  # 4 字节起始码
                end -= 1
        if end > pos:
            nals.append(data[pos:end])
        if nxt < 0:
            break
        pos = nxt + 3
    return nals


def _collect_parameter_sets(samples: Sequence[Tuple[int, int, bytes]],
                            start: int, limit: int) -> Tuple[List[bytes], List[bytes]]:
    """扫描样本，收集 SPS / PPS（去重、保序）。"""
    sps_list: List[bytes] = []
    pps_list: List[bytes] = []
    seen_sps = set()
    seen_pps = set()
    count = len(samples)
    if limit > count:
        limit = count
    for index in range(start, limit):
        for nal in _split_annex_b(samples[index][2]):
            if not nal:
                continue
            nal_type = nal[0] & 0x1F
            if nal_type == 7:
                if nal not in seen_sps:
                    seen_sps.add(nal)
                    sps_list.append(nal)
            elif nal_type == 8:
                if nal not in seen_pps:
                    seen_pps.add(nal)
                    pps_list.append(nal)
        if sps_list and pps_list:
            break
    return sps_list, pps_list


# --------------------------------------------------------------------------
# TS 解复用
# --------------------------------------------------------------------------


class _StreamSpec(object):
    """PMT 中声明的一条基本流。"""

    __slots__ = ("pid", "stream_type", "kind", "descriptors")

    def __init__(self, pid: int, stream_type: int, kind: str, descriptors: bytes) -> None:
        self.pid = pid
        self.stream_type = stream_type
        self.kind = kind  # "video" / "audio" / "hevc" / "unknown"
        self.descriptors = descriptors


class _PesAssembler(object):
    """把某个 PID 上的 TS 负载拼接成完整的 PES 包。"""

    __slots__ = ("_buf", "_expect", "_disc", "_last_cc", "_started")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._expect = -1
        self._disc = False
        self._last_cc = -1
        self._started = False

    def _finish(self, flush: bool) -> Optional[bytes]:
        buf = self._buf
        length = len(buf)
        if length >= 6:
            packet_length = _u16(buf, 4)
            if packet_length:
                total = packet_length + 6
                if total > length:
                    if not flush:
                        self._expect = total
                        return None
                    # 数据不完整，丢弃
                    self._buf = bytearray()
                    self._expect = -1
                    return None
                packet = bytes(buf[:total])
                self._buf = bytearray(buf[total:])
                self._expect = -1
                return packet
            # PES_packet_length == 0：不定长，遇到下一个 PUSI 结束
            if not flush:
                self._expect = -1
                return None
            packet = bytes(buf)
            self._buf = bytearray()
            self._expect = -1
            return packet
        if not flush:
            self._expect = -1
            return None
        packet = bytes(buf)
        self._buf = bytearray()
        self._expect = -1
        return packet

    def feed(self, payload: Any, pusi: bool, cc: int) -> List[bytes]:
        packets: List[bytes] = []
        if pusi:
            done = self._finish(flush=True)
            if done is not None:
                packets.append(done)
            self._buf = bytearray()
            self._expect = -1
            self._disc = False
            self._started = True
        elif not self._started:
            self._last_cc = cc
            return packets
        elif self._last_cc >= 0 and ((self._last_cc + 1) & 0x0F) != cc:
            # 丢包 -> 当前 PES 不可信，等下一个 PUSI 重新开始
            self._buf = bytearray()
            self._expect = -1
            self._disc = True
            self._last_cc = cc
            return packets

        self._last_cc = cc
        if not self._disc:
            self._buf += payload
            if self._expect > 0 and len(self._buf) >= self._expect:
                done = self._finish(flush=False)
                if done is not None:
                    packets.append(done)
        return packets

    def flush_all(self) -> List[bytes]:
        packet = self._finish(flush=True)
        return [packet] if packet is not None else []


#: 带标准 PES 扩展头（含 PTS/DTS）的 stream_id：音频 0xC0-0xDF、视频 0xE0-0xEF、
#: 以及 private_stream_1 (0xBD) / extended_stream_id (0xFD)
_PES_WITH_HEADER_IDS = frozenset(list(range(0xC0, 0xF0)) + [0xBD, 0xFD])


def _parse_pes_header(pes: bytes) -> Optional[Dict[str, Any]]:
    """解析 PES 头。"""
    if len(pes) < 9:
        return None
    if pes[0] != 0x00 or pes[1] != 0x00 or pes[2] != 0x01:
        return None
    stream_id = pes[3]
    if stream_id in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF):
        return None
    header_data_length = pes[8]
    header_len = 9 + header_data_length
    if header_len > len(pes):
        return None

    pts: Optional[int] = None
    dts: Optional[int] = None
    if stream_id in _PES_WITH_HEADER_IDS and header_data_length >= 5:
        flags = pes[7]
        pts_dts_flags = (flags >> 6) & 0x03
        if pts_dts_flags in (2, 3):
            raw = pes[9:14]
            pts = (((raw[0] >> 1) & 0x07) << 30) | (raw[1] << 22) | \
                  (((raw[2] >> 1) & 0x7F) << 15) | (raw[3] << 7) | \
                  ((raw[4] >> 1) & 0x7F)
        if pts_dts_flags == 3 and header_data_length >= 10:
            raw = pes[14:19]
            dts = (((raw[0] >> 1) & 0x07) << 30) | (raw[1] << 22) | \
                  (((raw[2] >> 1) & 0x7F) << 15) | (raw[3] << 7) | \
                  ((raw[4] >> 1) & 0x7F)
    if pts is None:
        return None
    if dts is None:
        dts = pts

    return {
        "stream_id": stream_id,
        "pts": pts,
        "dts": dts,
        "header_len": header_len,
        "payload": pes[header_len:],
    }


def _descriptor_stream_type(descriptors: bytes) -> Optional[int]:
    """从描述符推断 ``0x06`` 私有流的真实类型。"""
    pos = 0
    size = len(descriptors)
    while pos + 2 <= size:
        tag = descriptors[pos]
        length = descriptors[pos + 1]
        if pos + 2 + length > size:
            break
        if tag == 0x6A:  # AC-3
            return 0x81
        if tag == 0x7A:  # E-AC-3
            return 0x87
        if tag == 0x7B:  # DTS
            return 0x8A
        pos += 2 + length
    return None


class _Tables(object):
    """PAT / PMT 解析状态，以及基本流 PES 收集。"""

    def __init__(self) -> None:
        self.pat_buf = bytearray()
        self.pmt_bufs: Dict[int, bytearray] = {}
        self.pmt_pids: List[int] = []
        self.streams: Dict[int, _StreamSpec] = {}
        self.assemblers: Dict[int, _PesAssembler] = {}
        self.samples: Dict[int, List[Tuple[int, int, bytes]]] = {}
        self.pmt_done = False
        self.unknown_video = False
        self.unknown_stream_type: Optional[int] = None
        self.stop_requested = False
        self.video_pid = -1
        self.packets_seen = 0

    # -- PAT -------------------------------------------------------------
    def parse_pat(self, section: bytes) -> None:
        if len(section) < 12 or section[0] != 0x00:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        end = min(len(section), 3 + section_length - 4)
        pos = 8
        while pos + 4 <= end:
            program_number = _u16(section, pos)
            pid = ((section[pos + 2] & 0x1F) << 8) | section[pos + 3]
            if program_number != 0 and pid not in self.pmt_pids:
                self.pmt_pids.append(pid)
                self.pmt_bufs.setdefault(pid, bytearray())
            pos += 4

    # -- PMT -------------------------------------------------------------
    def parse_pmt(self, pid: int, section: bytes) -> None:
        if len(section) < 16 or section[0] != 0x02:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        end = min(len(section), 3 + section_length - 4)
        program_info_length = ((section[10] & 0x0F) << 8) | section[11]
        pos = 12 + program_info_length
        if pos > end:
            return
        while pos + 5 <= end:
            stream_type = section[pos]
            es_pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
            es_info_length = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
            desc_end = min(pos + 5 + es_info_length, end)
            self._register_stream(es_pid, stream_type, bytes(section[pos + 5:desc_end]))
            pos += 5 + es_info_length
        self.pmt_done = True

    def _register_stream(self, pid: int, stream_type: int, descriptors: bytes) -> None:
        if pid in self.streams:
            return
        kind = "unknown"
        if stream_type == ST_H264:
            kind = "video"
        elif stream_type == ST_HEVC:
            kind = "hevc"
        elif stream_type in (ST_AAC_ADTS, ST_AAC_LATM, ST_MPEG1_AUDIO, ST_MPEG2_AUDIO,
                             0x81, 0x87, 0x8A):
            kind = "audio"
        elif stream_type == ST_PRIVATE and _descriptor_stream_type(descriptors) is not None:
            kind = "audio"
        self.streams[pid] = _StreamSpec(pid, stream_type, kind, descriptors)
        if kind in ("video", "audio"):
            self.assemblers[pid] = _PesAssembler()
            self.samples[pid] = []
            if kind == "video" and self.video_pid < 0:
                self.video_pid = pid

    # -- PES -------------------------------------------------------------
    def on_pes(self, stream: _StreamSpec, packet: bytes) -> None:
        header = _parse_pes_header(packet)
        if header is None:
            return
        pts = header["pts"]
        dts = header["dts"]
        payload = header["payload"]
        if not payload:
            return
        samples = self.samples.get(stream.pid)
        if samples is None or len(samples) >= MAX_SAMPLES_PER_TRACK:
            self.stop_requested = True
            return
        if stream.kind == "video":
            if stream.stream_type == ST_HEVC:
                self.unknown_video = True
                self.unknown_stream_type = ST_HEVC
                self.stop_requested = True
                return
            if stream.stream_type != ST_H264:
                self.unknown_video = True
                self.unknown_stream_type = stream.stream_type
                self.stop_requested = True
                return
        elif stream.kind == "audio":
            if stream.stream_type in (ST_MPEG1_AUDIO, ST_MPEG2_AUDIO, 0x81, 0x87, 0x8A):
                return  # MP3 / AC-3 / DTS 暂不支持
            if stream.stream_type not in (ST_AAC_ADTS, ST_AAC_LATM) and \
                    not (len(payload) >= 2 and payload[0] == 0xFF and (payload[1] & 0xF0) == 0xF0):
                return
        else:
            return
        samples.append((int(pts), int(dts), payload))


def _scan_tables(buf: Any, tables: _Tables) -> None:
    """扫描一段 TS 数据，解析 PSI 并收集 PES。"""
    size = len(buf)
    limit = size - (size % TS_PACKET_SIZE)
    pos = 0
    pat_buf = tables.pat_buf
    pmt_bufs = tables.pmt_bufs
    assemblers = tables.assemblers
    streams = tables.streams
    packets_seen = tables.packets_seen

    while pos < limit:
        if buf[pos] != TS_SYNC_BYTE:
            pos += TS_PACKET_SIZE
            continue
        packets_seen += 1
        b1 = buf[pos + 1]
        pusi = (b1 & 0x40) != 0
        pid = ((b1 & 0x1F) << 8) | buf[pos + 2]
        b3 = buf[pos + 3]
        afc = (b3 >> 4) & 0x03
        if afc == 0 or afc == 2:
            pos += TS_PACKET_SIZE
            continue
        index = pos + 4
        if afc == 3:
            if index >= pos + TS_PACKET_SIZE:
                pos += TS_PACKET_SIZE
                continue
            index += 1 + buf[index]
            if index >= pos + TS_PACKET_SIZE:
                pos += TS_PACKET_SIZE
                continue
        payload = buf[index:pos + TS_PACKET_SIZE]
        cc = b3 & 0x0F

        if pid == 0:
            if pusi:
                if pat_buf:
                    tables.parse_pat(bytes(pat_buf))
                    del pat_buf[:]
                pat_buf += payload[1 + payload[0]:] if payload and 1 + payload[0] <= len(payload) else b""
            elif pat_buf:
                pat_buf += payload
            if len(pat_buf) > 4096:
                del pat_buf[:]
        elif pid in pmt_bufs:
            if pusi:
                existing = pmt_bufs[pid]
                if existing:
                    tables.parse_pmt(pid, bytes(existing))
                    del existing[:]
                pmt_bufs[pid] = bytearray(
                    payload[1 + payload[0]:] if payload and 1 + payload[0] <= len(payload) else b""
                )
            else:
                existing = pmt_bufs.get(pid)
                if existing is not None:
                    existing += payload
                    if len(existing) > 4096:
                        del existing[:]
        else:
            assembler = assemblers.get(pid)
            if assembler is not None:
                stream = streams.get(pid)
                if stream is not None:
                    for packet in assembler.feed(payload, pusi, cc):
                        tables.on_pes(stream, packet)
                        if tables.stop_requested:
                            break
        if tables.stop_requested:
            break
        pos += TS_PACKET_SIZE
    tables.packets_seen = packets_seen


def _find_sync_offset(path: str, window: int = 1 << 16) -> int:
    """找 TS 起始偏移（正常为 0；个别文件带 192 字节包头）。"""
    with open(path, "rb") as handle:
        head = handle.read(window)
    size = len(head)
    if size >= TS_PACKET_SIZE and head[0] == TS_SYNC_BYTE and \
            (size < TS_PACKET_SIZE * 2 or head[TS_PACKET_SIZE] == TS_SYNC_BYTE):
        return 0
    first = head.find(b"\x47")
    if first < 0:
        return 0
    pos = first
    limit = min(size - TS_PACKET_SIZE * 2, first + TS_PACKET_SIZE * 6)
    while pos < limit:
        if head[pos + TS_PACKET_SIZE] == TS_SYNC_BYTE and \
                head[pos + TS_PACKET_SIZE * 2] == TS_SYNC_BYTE:
            return pos
        pos += 1
    return first if first < TS_PACKET_SIZE else 0


def _demux(path: str, tables: _Tables, max_passes: int = 2) -> None:
    """顺序扫描 TS，填充 ``tables``。"""
    total = os.path.getsize(path)
    if total == 0:
        raise RemuxError("TS 文件为空")
    offset = _find_sync_offset(path)
    usable = total - offset
    if usable < TS_PACKET_SIZE * 2:
        raise RemuxError("TS 文件过短或不是 TS 格式")

    for _pass in range(max_passes):
        with open(path, "rb") as handle:
            handle.seek(offset)
            pending = b""
            while True:
                block = handle.read(READ_CHUNK)
                if not block:
                    break
                if pending:
                    block = pending + block
                    pending = b""
                aligned = len(block) - (len(block) % TS_PACKET_SIZE)
                if aligned:
                    _scan_tables(memoryview(block)[:aligned], tables)
                    pending = block[aligned:]
                else:
                    pending = block
                if tables.stop_requested:
                    break
        for pid, assembler in tables.assemblers.items():
            stream = tables.streams.get(pid)
            if stream is None or tables.stop_requested:
                continue
            for packet in assembler.flush_all():
                tables.on_pes(stream, packet)
        if tables.stop_requested or _tables_complete(tables):
            break
    if tables.packets_seen == 0:
        raise RemuxError("没有找到任何 TS 同步包")


def _tables_complete(tables: _Tables) -> bool:
    """视频样本已出现就没必要再扫第二趟（第二趟只是兜底）。"""
    if tables.video_pid < 0:
        return False
    return bool(tables.samples.get(tables.video_pid))


# --------------------------------------------------------------------------
# ADTS / AAC
# --------------------------------------------------------------------------


class _AacConfig(object):
    __slots__ = ("object_type", "sample_rate_index", "sample_rate", "channels")

    def __init__(self, object_type: int, sample_rate_index: int,
                 sample_rate: int, channels: int) -> None:
        self.object_type = object_type
        self.sample_rate_index = sample_rate_index
        self.sample_rate = sample_rate
        self.channels = channels

    def audio_specific_config(self) -> bytes:
        """AudioSpecificConfig（常规 2 字节情形）。"""
        value = ((self.object_type & 0x1F) << 11) | \
                ((self.sample_rate_index & 0x0F) << 7) | \
                ((self.channels & 0x0F) << 3)
        return struct.pack(">H", value & 0xFFFF)


def _parse_adts_header(data: bytes) -> Optional[Tuple[_AacConfig, int, int]]:
    """解析 ADTS 头，返回 ``(config, header_len, frame_length)``。"""
    if len(data) < 7:
        return None
    if data[0] != 0xFF or (data[1] & 0xF0) != 0xF0:
        return None
    protection_absent = data[1] & 0x01
    profile = (data[2] >> 6) & 0x03
    sample_rate_index = (data[2] >> 2) & 0x0F
    if sample_rate_index >= len(AAC_SAMPLE_RATES):
        return None
    channels = ((data[2] & 0x01) << 2) | ((data[3] >> 6) & 0x03)
    frame_length = ((data[3] & 0x03) << 11) | (data[4] << 3) | ((data[5] >> 5) & 0x07)
    header_len = 7 if protection_absent else 9
    if frame_length < header_len:
        return None
    config = _AacConfig(profile + 1, sample_rate_index,
                        AAC_SAMPLE_RATES[sample_rate_index], channels)
    return config, header_len, frame_length


# --------------------------------------------------------------------------
# 样本与时间轴
# --------------------------------------------------------------------------


class _Sample(object):
    __slots__ = ("data", "dts", "cts_offset", "duration", "sync", "chunk")

    def __init__(self, data: bytes, dts: int, cts_offset: int, sync: bool) -> None:
        self.data = data
        self.dts = dts
        self.cts_offset = cts_offset
        self.duration = 0
        self.sync = sync
        self.chunk = -1


def _build_track(samples: List[_Sample], timescale: int, fixed_duration: int = 0) -> int:
    """计算每个样本时长，返回轨道媒体时长（timescale 单位）。"""
    count = len(samples)
    if count == 0:
        return 0

    if fixed_duration > 0:
        for index in range(count - 1):
            samples[index].duration = fixed_duration
        tail_duration = fixed_duration
        if count > 1:
            tail = samples[count - 1].dts - samples[count - 2].dts
            if 0 < tail <= timescale * MAX_SANE_DELTA:
                tail_duration = tail
        samples[count - 1].duration = tail_duration
        return samples[count - 1].dts + tail_duration

    deltas: List[int] = []
    for index in range(count - 1):
        delta = samples[index + 1].dts - samples[index].dts
        if 0 < delta <= timescale * MAX_SANE_DELTA:
            deltas.append(delta)
    fallback = int(_median(deltas)) if deltas else int(timescale * DEFAULT_FRAME_DURATION)
    if fallback <= 0:
        fallback = max(1, int(timescale * DEFAULT_FRAME_DURATION))

    for index in range(count - 1):
        delta = samples[index + 1].dts - samples[index].dts
        samples[index].duration = delta if 0 < delta <= timescale * MAX_SANE_DELTA else fallback
    tail = fallback
    if count > 1:
        delta = samples[count - 1].dts - samples[count - 2].dts
        if 0 < delta <= timescale * MAX_SANE_DELTA:
            tail = delta
    samples[count - 1].duration = tail
    return samples[count - 1].dts + tail


# --------------------------------------------------------------------------
# MP4 box 构造
# --------------------------------------------------------------------------


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    header = bytes((version, (flags >> 16) & 0xFF, (flags >> 8) & 0xFF, flags & 0xFF))
    return _box(box_type, header + payload)


def _make_ftyp() -> bytes:
    return _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2avc1mp41")


def _make_avcc(sps_list: List[bytes], pps_list: List[bytes]) -> bytes:
    if not sps_list or not pps_list:
        raise RemuxError("缺少 SPS/PPS，无法构造 avcC")
    sps = sps_list[0]
    payload = bytearray()
    payload.append(1)  # configurationVersion
    payload.append(sps[1] if len(sps) > 1 else 0x42)
    payload.append(sps[2] if len(sps) > 2 else 0x00)
    payload.append(sps[3] if len(sps) > 3 else 0x1F)
    payload.append(0xFF)  # reserved(6) + lengthSizeMinusOne(2) = 3
    payload.append(0xE0 | (len(sps_list) & 0x1F))
    for nal in sps_list:
        payload += struct.pack(">H", len(nal))
        payload += nal
    payload.append(len(pps_list) & 0xFF)
    for nal in pps_list:
        payload += struct.pack(">H", len(nal))
        payload += nal
    return _box(b"avcC", bytes(payload))


def _make_esds(config: _AacConfig, avg_bitrate: int, max_bitrate: int) -> bytes:
    if config.sample_rate_index == 15:
        head = ((config.object_type & 0x1F) << 11) | (0x0F << 7) | \
               ((config.channels & 0x0F) << 3)
        dsi = struct.pack(">I", head << 8)[:3] + struct.pack(">I", config.sample_rate)[1:]
    else:
        dsi = config.audio_specific_config()
    dec_specific = bytes((0x05, len(dsi))) + dsi
    decoder_config = bytes((0x04, 13 + len(dec_specific), 0x40, 0x15)) + \
        b"\x00\x00\x00" + struct.pack(">I", max_bitrate) + \
        struct.pack(">I", avg_bitrate) + dec_specific
    sl_config = bytes((0x06, 0x01, 0x02))
    es_payload = struct.pack(">H", 1) + b"\x00" + decoder_config + sl_config
    return _full_box(b"esds", 0, 0, bytes((0x03, len(es_payload))) + es_payload)


def _make_stsd_video(width: int, height: int, avcc: bytes) -> bytes:
    entry = bytearray()
    entry += b"\x00" * 6
    entry += struct.pack(">H", 1)  # data_reference_index
    entry += b"\x00" * 16
    entry += struct.pack(">HH", width, height)
    entry += struct.pack(">II", 0x00480000, 0x00480000)  # 72 dpi
    entry += struct.pack(">I", 0)
    entry += struct.pack(">H", 1)  # frame_count
    entry += b"\x00" * 32  # compressorname
    entry += struct.pack(">H", 0x0018)  # depth
    entry += struct.pack(">h", -1)
    entry += avcc
    # stsd: version/flags 由 _full_box 写入，随后是 entry_count
    payload = struct.pack(">I", 1) + _box(b"avc1", bytes(entry))
    return _full_box(b"stsd", 0, 0, payload)


def _make_stsd_audio(channels: int, sample_rate: int, sample_size: int,
                     esds: bytes) -> bytes:
    entry = bytearray()
    entry += b"\x00" * 6
    entry += struct.pack(">H", 1)
    entry += struct.pack(">II", 0, 0)
    entry += struct.pack(">HH", channels, sample_size)
    entry += b"\x00\x00\x00\x00"
    entry += struct.pack(">I", (sample_rate & 0xFFFF) << 16)
    entry += esds
    payload = struct.pack(">I", 1) + _box(b"mp4a", bytes(entry))
    return _full_box(b"stsd", 0, 0, payload)


def _make_stts(samples: List[_Sample]) -> bytes:
    entries: List[List[int]] = []
    for sample in samples:
        if entries and entries[-1][1] == sample.duration:
            entries[-1][0] += 1
        else:
            entries.append([1, sample.duration])
    payload = bytearray(struct.pack(">I", len(entries)))
    for count, delta in entries:
        payload += struct.pack(">II", count, delta)
    return _full_box(b"stts", 0, 0, bytes(payload))


def _make_ctts(samples: List[_Sample]) -> Optional[bytes]:
    if not any(sample.cts_offset for sample in samples):
        return None
    entries: List[List[int]] = []
    for sample in samples:
        if entries and entries[-1][1] == sample.cts_offset:
            entries[-1][0] += 1
        else:
            entries.append([1, sample.cts_offset])
    payload = bytearray(struct.pack(">I", len(entries)))
    for count, offset in entries:
        payload += struct.pack(">Ii", count, offset)
    return _full_box(b"ctts", 0, 0, bytes(payload))


def _make_stsz(samples: List[_Sample]) -> bytes:
    payload = bytearray()
    payload += struct.pack(">I", 0)  # sample_size：逐个给出
    payload += struct.pack(">I", len(samples))
    for sample in samples:
        payload += struct.pack(">I", len(sample.data))
    return _full_box(b"stsz", 0, 0, bytes(payload))


def _make_stsc(samples: List[_Sample]) -> bytes:
    entries: List[List[int]] = []
    for sample in samples:
        first_chunk = sample.chunk + 1  # stsc 的 first_chunk 从 1 开始
        if entries and entries[-1][0] == first_chunk:
            entries[-1][1] += 1
        else:
            entries.append([first_chunk, 1])
    payload = bytearray(struct.pack(">I", len(entries)))
    for first_chunk, per_chunk in entries:
        payload += struct.pack(">III", first_chunk, per_chunk, 1)
    return _full_box(b"stsc", 0, 0, bytes(payload))


def _make_stco(offsets: List[int], co64: bool) -> bytes:
    payload = bytearray(struct.pack(">I", len(offsets)))
    if co64:
        for offset in offsets:
            payload += struct.pack(">Q", offset)
        return _full_box(b"co64", 0, 0, bytes(payload))
    for offset in offsets:
        payload += struct.pack(">I", offset)
    return _full_box(b"stco", 0, 0, bytes(payload))


def _make_stss(samples: List[_Sample], force: bool) -> Optional[bytes]:
    indexes = [index + 1 for index, sample in enumerate(samples) if sample.sync]
    if not indexes:
        raise RemuxError("视频轨道没有任何同步样本")
    if len(indexes) == len(samples) and not force:
        return None
    payload = bytearray(struct.pack(">I", len(indexes)))
    for index in indexes:
        payload += struct.pack(">I", index)
    return _full_box(b"stss", 0, 0, bytes(payload))


def _make_dinf() -> bytes:
    dref = _full_box(b"dref", 0, 0,
                     struct.pack(">I", 1) + _full_box(b"url ", 0, 1, b""))
    return _box(b"dinf", dref)


def _make_elst(media_duration_movie: int, start_delay_movie: int) -> bytes:
    entries = bytearray()
    count = 0
    if start_delay_movie > 0:
        # 空编辑：表示该轨道延迟开始
        entries += struct.pack(">IIh", start_delay_movie, 0xFFFFFFFF, 1)
        entries += struct.pack(">h", 0)
        count += 1
    entries += struct.pack(">IIh", media_duration_movie, 0, 1)
    entries += struct.pack(">h", 0)
    count += 1
    return _full_box(b"elst", 0, 0, struct.pack(">I", count) + bytes(entries))


def _make_trak(samples: List[_Sample], chunk_offsets: List[int], timescale: int,
               duration: int, track_id: int, is_video: bool, media: bytes,
               co64: bool, width: int, height: int, start_delay_movie: int,
               track_duration_movie: int, force_stss: bool) -> bytes:
    tkhd_payload = bytearray()
    tkhd_payload += struct.pack(">IIIII", 0, 0, track_id, 0, track_duration_movie)
    tkhd_payload += b"\x00" * 8  # reserved
    # layer(2) alternate_group(2) volume(2) reserved(2)
    tkhd_payload += struct.pack(">hhhh", 0, 0, 0 if is_video else 0x0100, 0)
    tkhd_payload += struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    tkhd_payload += struct.pack(">II", width << 16, height << 16)
    tkhd = _full_box(b"tkhd", 0, 0x000003, bytes(tkhd_payload))

    mdhd_payload = struct.pack(">IIII", 0, 0, timescale, duration) + \
        struct.pack(">HH", UND_LANGUAGE, 0)
    mdhd = _full_box(b"mdhd", 0, 0, mdhd_payload)

    handler = b"vide" if is_video else b"soun"
    name = b"VideoHandler" if is_video else b"SoundHandler"
    hdlr = _full_box(b"hdlr", 0, 0,
                     struct.pack(">I", 0) + handler + b"\x00" * 12 + name + b"\x00")

    stbl_children = [media, _make_stts(samples)]
    ctts = _make_ctts(samples)
    if ctts is not None:
        stbl_children.append(ctts)
    if is_video:
        stss = _make_stss(samples, force_stss)
        if stss is not None:
            stbl_children.append(stss)
    stbl_children.append(_make_stsc(samples))
    stbl_children.append(_make_stsz(samples))
    stbl_children.append(_make_stco(chunk_offsets, co64))
    stbl = _box(b"stbl", b"".join(stbl_children))

    if is_video:
        minf_children = [_full_box(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))]
    else:
        minf_children = [_full_box(b"smhd", 0, 0, struct.pack(">hH", 0, 0))]
    minf_children.append(_make_dinf())
    minf_children.append(stbl)
    minf = _box(b"minf", b"".join(minf_children))

    mdia = _box(b"mdia", mdhd + hdlr + minf)
    edts = _box(b"edts", _make_elst(track_duration_movie, start_delay_movie))
    return _box(b"trak", tkhd + edts + mdia)


def _make_mvhd(duration_movie: int, next_track_id: int) -> bytes:
    payload = bytearray()
    payload += struct.pack(">II", 0, 0)
    payload += struct.pack(">I", MOVIE_TIMESCALE)
    payload += struct.pack(">I", duration_movie)
    payload += struct.pack(">I", 0x00010000)  # rate
    payload += struct.pack(">H", 0x0100)  # volume
    payload += b"\x00\x00"
    payload += b"\x00" * 8
    payload += struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    payload += b"\x00" * 24
    payload += struct.pack(">I", next_track_id)
    return _full_box(b"mvhd", 0, 0, bytes(payload))


# --------------------------------------------------------------------------
# 轨道准备
# --------------------------------------------------------------------------


def _prepare_video(samples: Sequence[Tuple[int, int, bytes]]) -> Dict[str, Any]:
    """视频 PES 列表 -> MP4 样本列表 + avcC。"""
    if not samples:
        raise RemuxError("没有视频样本")

    sps_list, pps_list = _collect_parameter_sets(samples, 0, min(512, len(samples)))
    if not sps_list:
        sps_list, pps_list = _collect_parameter_sets(samples, 0, len(samples))
    if not sps_list:
        raise RemuxError("码流中没有找到 SPS（可能不是 H.264）")
    if not pps_list:
        raise RemuxError("码流中没有找到 PPS")
    info = _parse_sps(sps_list[0])

    seen_sps = set(sps_list)
    seen_pps = set(pps_list)
    out: List[_Sample] = []
    for pts, dts, payload in samples:
        buf = bytearray()
        is_idr = False
        for nal in _split_annex_b(payload):
            if not nal:
                continue
            nal_type = nal[0] & 0x1F
            if nal_type == 7:
                if nal not in seen_sps:
                    seen_sps.add(nal)
                    sps_list.append(nal)
                continue
            if nal_type == 8:
                if nal not in seen_pps:
                    seen_pps.add(nal)
                    pps_list.append(nal)
                continue
            if nal_type in (9, 10, 11, 12):  # AUD / 序列结束 / 流结束 / 填充
                continue
            if nal_type == 5:
                is_idr = True
            buf += struct.pack(">I", len(nal))
            buf += nal
        if not buf:
            continue
        cts_offset = int(pts) - int(dts)
        if cts_offset < 0:
            cts_offset = 0
        out.append(_Sample(bytes(buf), int(dts), cts_offset, is_idr))

    if not out:
        raise RemuxError("视频样本为空（没有可用的 NAL）")

    return {
        "samples": out,
        "avcc": _make_avcc(sps_list, pps_list),
        "info": info,
        "codec": _codec_string(info),
        "timescale": VIDEO_TIMESCALE,
        "is_video": True,
        "frame_duration": 0,  # 用相邻 DTS 差值作为样本时长
    }


def _prepare_audio(samples: Sequence[Tuple[int, int, bytes]]) -> Optional[Dict[str, Any]]:
    """音频 PES 列表 -> MP4 样本列表 + esds 参数。"""
    if not samples:
        return None
    config: Optional[_AacConfig] = None
    out: List[_Sample] = []
    total_bytes = 0
    for pts, _dts, payload in samples:
        offset = 0
        size = len(payload)
        frame_index = 0
        while offset + 7 <= size:
            header = _parse_adts_header(payload[offset:offset + 7])
            if header is None:
                nxt = payload.find(b"\xff", offset + 1)
                while nxt >= 0 and nxt + 2 <= size:
                    if payload[nxt] == 0xFF and (payload[nxt + 1] & 0xF0) == 0xF0:
                        break
                    nxt = payload.find(b"\xff", nxt + 1)
                if nxt < 0 or nxt + 7 > size:
                    break
                offset = nxt
                continue
            frame_config, header_len, frame_length = header
            if offset + frame_length > size:
                break
            if config is None:
                config = frame_config
            raw = payload[offset + header_len:offset + frame_length]
            if raw:
                # 音频样本的 DTS 直接用采样率为时间基，保证每帧恰好 1024
                dts = int(round(pts * config.sample_rate / float(VIDEO_TIMESCALE))) + \
                    frame_index * 1024
                out.append(_Sample(raw, dts, 0, True))
                total_bytes += len(raw)
                frame_index += 1
            offset += frame_length
            if len(out) >= MAX_SAMPLES_PER_TRACK:
                break
        if len(out) >= MAX_SAMPLES_PER_TRACK:
            break

    if config is None or not out:
        return None

    media_duration = _build_track(out, config.sample_rate, fixed_duration=1024)
    avg_bitrate = 0
    if media_duration > 0:
        avg_bitrate = int(total_bytes * 8 * config.sample_rate / media_duration)
    avg_bitrate = max(1, min(avg_bitrate, 0x7FFFFFFF))
    return {
        "samples": out,
        "config": config,
        "duration": media_duration,
        "timescale": config.sample_rate,
        "codec": "mp4a.40.%d" % config.object_type,
        "avg_bitrate": avg_bitrate,
        "raw_bytes": total_bytes,
        "is_video": False,
        "frame_duration": 1024,  # AAC 每帧固定 1024 个采样点
    }


# --------------------------------------------------------------------------
# 时间轴归一 + chunk 交织
# --------------------------------------------------------------------------


def _normalize_timeline(tracks: List[Dict[str, Any]]) -> Tuple[int, int]:
    """把各轨道归一到公共起点，返回 ``(movie_duration, 全局下一 track_id)``。

    每条轨道的 DTS 减去自身首个 DTS（stts 从 0 开始）；起始较晚的轨道用
    空 edit list 表达延迟，从而保持音画同步。
    """
    if not tracks:
        raise RemuxError("没有任何轨道")
    starts: List[float] = []
    for track in tracks:
        samples = track["samples"]
        base = samples[0].dts
        timescale = track["timescale"]
        for sample in samples:
            sample.dts -= base
        track["first_pts"] = base
        starts.append(base / float(timescale))
    origin = min(starts)

    duration_movie = 0
    for track, start in zip(tracks, starts):
        timescale = track["timescale"]
        media_duration = _build_track(track["samples"], timescale,
                                      fixed_duration=track.get("frame_duration", 0))
        track["duration"] = media_duration
        delay_sec = start - origin
        delay_movie = int(round(delay_sec * MOVIE_TIMESCALE))
        if delay_movie < 2:
            delay_movie = 0
        media_movie = int(round(media_duration * MOVIE_TIMESCALE / float(timescale)))
        track["start_delay_movie"] = delay_movie
        track["duration_movie"] = media_movie
        if delay_movie + media_movie > duration_movie:
            duration_movie = delay_movie + media_movie
    return duration_movie, len(tracks) + 1


def _build_chunks(tracks: List[Dict[str, Any]]) -> List[Tuple[float, int, int, int]]:
    """把每条轨道的样本切成约 0.5 秒的 chunk，并按时间排序。

    返回 ``[(start_seconds, track_index, first_sample, last_sample_exclusive), ...]``。
    使用固定样本数上限（而不是按时间戳切分），保证每个解码周期的字节量
    可控——HLS 码流里偶尔会有时间戳重复的帧，纯按时间切会让某个 chunk 膨胀。
    """
    chunks: List[Tuple[float, int, int, int]] = []
    for track_index, track in enumerate(tracks):
        samples = track["samples"]
        timescale = track["timescale"]
        duration = track["duration"]
        count = len(samples)
        if count <= 0:
            continue
        target = int(round(count * timescale * CHUNK_SECONDS / float(duration))) \
            if duration > 0 else 64
        span = max(4, min(target, 128))
        start = 0
        while start < count:
            end = start + span
            if end > count:
                end = count
            chunks.append((samples[start].dts / float(timescale), track_index, start, end))
            start = end
    chunks.sort(key=lambda item: (item[0], item[1], item[2]))
    return chunks


def _assign_offsets(tracks: List[Dict[str, Any]],
                    chunks: List[Tuple[float, int, int, int]],
                    mdat_start: int) -> int:
    """按 chunk 顺序分配 mdat 偏移，返回 mdat 总大小。"""
    for track in tracks:
        track["chunk_offsets"] = []
    cursor = mdat_start + 8
    for _start_sec, track_index, first, last in chunks:
        track = tracks[track_index]
        track["chunk_offsets"].append(cursor)
        chunk_index = len(track["chunk_offsets"]) - 1
        samples = track["samples"]
        total = 0
        for index in range(first, last):
            samples[index].chunk = chunk_index
            total += len(samples[index].data)
        cursor += total
    return cursor - mdat_start


def _build_moov(tracks: List[Dict[str, Any]], duration_movie: int,
                next_track_id: int, use_co64: bool) -> bytes:
    traks = []
    track_id = 1
    for track in tracks:
        is_video = track.get("is_video", False)
        if is_video:
            info = track["info"]
            media = _make_stsd_video(info["width"], info["height"], track["avcc"])
            width, height = info["width"], info["height"]
        else:
            config = track["config"]
            media = _make_stsd_audio(config.channels, config.sample_rate, 16,
                                     _make_esds(config, track["avg_bitrate"],
                                                track["avg_bitrate"]))
            width, height = 0, 0
        traks.append(_make_trak(
            track["samples"], track["chunk_offsets"], track["timescale"],
            track["duration"], track_id, is_video, media, use_co64, width, height,
            track["start_delay_movie"], track["duration_movie"],
            track.get("force_stss", False),
        ))
        track_id += 1
    return _box(b"moov", _make_mvhd(duration_movie, next_track_id) + b"".join(traks))


# --------------------------------------------------------------------------
# 写文件
# --------------------------------------------------------------------------


def _write_output(path: str, tracks: List[Dict[str, Any]], faststart: bool) -> None:
    if not tracks:
        raise RemuxError("没有可写出的轨道")
    duration_movie, next_track_id = _normalize_timeline(tracks)
    chunks = _build_chunks(tracks)
    if not chunks:
        raise RemuxError("没有可写出的 chunk")

    ftyp = _make_ftyp()
    total_media = sum(len(sample.data) for track in tracks for sample in track["samples"])
    # co64 的选择只取决于总大小上界，从而与偏移量无关，保证一次收敛
    max_offset = len(ftyp) + total_media + 8 + (1 << 20)
    use_co64 = max_offset > 0xFFFFFFFF

    _assign_offsets(tracks, chunks, len(ftyp))
    moov = _build_moov(tracks, duration_movie, next_track_id, use_co64)
    mdat_start = len(ftyp) + len(moov) if faststart else len(ftyp)
    mdat_size = _assign_offsets(tracks, chunks, mdat_start)
    moov2 = _build_moov(tracks, duration_movie, next_track_id, use_co64)
    if len(moov2) != len(moov):  # 保险起见再迭代一次
        moov = moov2
        mdat_start = len(ftyp) + len(moov) if faststart else len(ftyp)
        mdat_size = _assign_offsets(tracks, chunks, mdat_start)
        moov2 = _build_moov(tracks, duration_movie, next_track_id, use_co64)
        if len(moov2) != len(moov):
            raise RemuxError("moov 大小不稳定，无法确定 mdat 偏移")
    moov = moov2

    header_size = len(ftyp) + (len(moov) if faststart else 0) + 8
    if faststart:
        for track in tracks:
            for offset in track["chunk_offsets"]:
                if offset < header_size:
                    raise RemuxError("chunk 偏移非法")
    expected_size = len(ftyp) + len(moov) + 8 + total_media
    if mdat_size != total_media + 8:
        raise RemuxError("mdat 大小与样本总大小不一致")

    tmp_path = path + ".part"
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(ftyp)
            if faststart:
                handle.write(moov)
            handle.write(struct.pack(">I", mdat_size) + b"mdat")
            for _start_sec, track_index, first, last in chunks:
                samples = tracks[track_index]["samples"]
                for index in range(first, last):
                    handle.write(samples[index].data)
            if not faststart:
                handle.write(moov)
        if os.path.getsize(tmp_path) != expected_size:
            raise RemuxError("输出文件大小校验失败")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def _cleanup(path: str) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 公共 API
# --------------------------------------------------------------------------


def ts_to_mp4(ts_path: str, mp4_path: str, *, faststart: bool = True) -> bool:
    """把 MPEG-TS 重封装为 MP4。成功返回 ``True``，失败返回 ``False``。

    * 只会抛出 :class:`RemuxError`（实际上内部已捕获并转成 ``False``），
      其它任何异常也一律捕获后返回 ``False``。
    * 失败时会删除半成品 ``mp4_path``。
    * 没有音频但视频正常时，输出纯视频 MP4 并返回 ``True``。
    """
    try:
        if not ts_path or not mp4_path:
            return False
        if not os.path.isfile(ts_path):
            return False
        if os.path.getsize(ts_path) < TS_PACKET_SIZE * 2:
            return False

        tables = _Tables()
        _demux(ts_path, tables)

        if tables.unknown_video:
            raise RemuxError("不支持的视频编码 stream_type=0x%02X" %
                             (tables.unknown_stream_type or 0))
        for stream in tables.streams.values():
            if stream.kind == "hevc":
                raise RemuxError("不支持 HEVC/H.265 视频（stream_type=0x24）")
        video_pid = tables.video_pid
        if video_pid < 0:
            return False
        video_raw = tables.samples.get(video_pid)
        if not video_raw:
            return False

        video = _prepare_video(list(video_raw))
        video["is_video"] = True
        video_raw.clear()

        tracks: List[Dict[str, Any]] = [video]
        for pid, stream in tables.streams.items():
            if stream.kind != "audio" or pid == video_pid:
                continue
            raw = tables.samples.get(pid)
            if not raw:
                continue
            audio = _prepare_audio(list(raw))
            raw.clear()
            if audio is not None and audio["samples"]:
                tracks.append(audio)
                break

        directory = os.path.dirname(os.path.abspath(mp4_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

        _write_output(mp4_path, tracks, bool(faststart))
        return True
    except RemuxError:
        _cleanup(mp4_path)
        return False
    except (OSError, ValueError, IndexError, KeyError, TypeError,
            struct.error, OverflowError, MemoryError, ZeroDivisionError):
        _cleanup(mp4_path)
        return False
    except BaseException:
        _cleanup(mp4_path)
        return False
    finally:
        if isinstance(mp4_path, str) and mp4_path:
            _cleanup(mp4_path + ".part")


def probe_ts(ts_path: str) -> Dict[str, Any]:
    """探测 TS 文件，返回编码 / 分辨率 / 采样率 / 时长等信息。

    返回形如::

        {'video': {'codec': 'avc1.64001f', 'width': 848, 'height': 480, 'fps': 30.0},
         'audio': {'codec': 'mp4a.40.2', 'sample_rate': 48000, 'channels': 2},
         'duration': 60.0, 'streams': 2}

    没有音频时 ``'audio'`` 为 ``None``；没有视频时 ``'video'`` 为 ``None``。
    """
    result: Dict[str, Any] = {"video": None, "audio": None, "duration": 0.0, "streams": 0}
    try:
        if not ts_path or not os.path.isfile(ts_path):
            return result
        if os.path.getsize(ts_path) < TS_PACKET_SIZE * 2:
            return result

        tables = _Tables()
        _demux(ts_path, tables)
        streams = 0
        video_span = 0.0

        video_pid = tables.video_pid
        video_samples = tables.samples.get(video_pid) if video_pid >= 0 else None
        if video_samples:
            sps_list, _pps = _collect_parameter_sets(video_samples, 0,
                                                     min(512, len(video_samples)))
            if not sps_list:
                sps_list, _pps = _collect_parameter_sets(video_samples, 0,
                                                         len(video_samples))
            if sps_list:
                try:
                    info = _parse_sps(sps_list[0])
                except RemuxError:
                    info = None
                if info is not None:
                    span = (video_samples[-1][1] - video_samples[0][1]) / float(VIDEO_TIMESCALE)
                    video_span = span if span > 0 else 0.0
                    fps = 0.0
                    if len(video_samples) > 1 and video_span > 0:
                        fps = (len(video_samples) - 1) / video_span
                    if fps <= 0 or fps > 240:
                        fps = 0.0
                    result["video"] = {
                        "codec": _codec_string(info),
                        "width": info["width"],
                        "height": info["height"],
                        "fps": round(fps, 3),
                    }
                    streams += 1

        audio_duration = 0.0
        for pid, stream in tables.streams.items():
            if stream.kind != "audio":
                continue
            raw = tables.samples.get(pid)
            if not raw:
                continue
            config = None
            for _pts, _dts, payload in raw[:64]:
                for offset in range(0, max(1, min(len(payload) - 6, 64))):
                    header = _parse_adts_header(payload[offset:offset + 7])
                    if header is not None:
                        config = header[0]
                        break
                if config is not None:
                    break
            if config is None:
                continue
            result["audio"] = {
                "codec": "mp4a.40.%d" % config.object_type,
                "sample_rate": config.sample_rate,
                "channels": config.channels,
            }
            audio_duration = len(raw) * 1024 / float(config.sample_rate)
            streams += 1
            break

        result["streams"] = streams
        duration = video_span if video_span > 0 else audio_duration
        result["duration"] = round(max(0.0, duration), 3)
        return result
    except RemuxError:
        return result
    except (OSError, ValueError, IndexError, KeyError, TypeError,
            struct.error, OverflowError, MemoryError, ZeroDivisionError):
        return result
    except BaseException:
        return result
