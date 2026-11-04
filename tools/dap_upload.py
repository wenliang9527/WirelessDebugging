# -*- coding: utf-8 -*-
"""dap_upload.py — 离线烧录:WiFi 镜像上传客户端(TCP:1235)

与固件 dap_upload.c 的帧协议对齐(00_preparation/产品化方案-离线烧录.md §6):
    [type u8][len u16le][rfu u8] + payload[len]
    BEGIN 0x01  slot u8 | name[32] | size u32 | crc32 u32 | family[8] | addr u32
    CHUNK 0x02  seq u32 | data(≤16KB)
    ACK   0x03  seq u32 | status u8      (CHUNK 必答,滑窗=1)
    FINISH 0x04 (空)
    RESULT 0x05 status u8 | msg[64]

status: 0=OK 1=BUSY 2=BAD_ARG 3=IO 4=SIZE 5=CRC 6=PROTOCOL

上传过程在调用方的工作线程里同步执行;progress 回调(0..100)由调用方
决定怎么投递回 UI(本模块不碰界面)。
"""
import socket
import struct
import zlib
from typing import Callable, Optional

PORT = 1235
CHUNK = 16 * 1024
_HDR = struct.Struct("<BHB")           # type, len, rfu
_BEGIN = struct.Struct("<B32sII8sI")   # slot, name, size, crc32, family, addr
_U32 = struct.Struct("<I")

FT_BEGIN, FT_CHUNK, FT_ACK, FT_FINISH, FT_RESULT = 0x01, 0x02, 0x03, 0x04, 0x05
ST_OK, ST_BUSY, ST_BAD_ARG, ST_IO, ST_SIZE, ST_CRC, ST_PROTOCOL = range(7)

_STATUS_TEXT = {
    ST_BUSY: "板上已有上传会话占着(另一路 @FPUT 没收尾),稍后再试",
    ST_BAD_ARG: "板上拒绝了元数据(名字/家族/槽位不合法)",
    ST_IO: "板上写闪存失败(空间不足?换一个空槽)",
    ST_SIZE: "字节数与声明不符",
    ST_CRC: "CRC 校验不过(链路差或数据损坏),重传一次",
    ST_PROTOCOL: "协议帧错位(工具与固件版本不匹配)",
}


class UploadError(Exception):
    """上传失败;message 已是人话,直接给结果面板。"""

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


def _send_frame(sock: socket.socket, ftype: int, payload: bytes = b"") -> None:
    sock.sendall(_HDR.pack(ftype, len(payload), 0) + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        piece = sock.recv(n - len(buf))
        if not piece:
            raise UploadError("连接被板子断开(上传中断)")
        buf += piece
    return bytes(buf)


def _recv_frame(sock: socket.socket):
    hdr = _recv_exact(sock, _HDR.size)
    ftype, length, _ = _HDR.unpack(hdr)
    payload = _recv_exact(sock, length) if length else b""
    return ftype, payload


def upload_image(host: str, slot: int, name: str, family: str, addr: int,
                 data: bytes,
                 progress: Optional[Callable[[int], None]] = None,
                 port: int = PORT, timeout: float = 10.0) -> str:
    """把一个镜像完整传进槽位。成功返回板端 RESULT 的消息(通常 "saved")。

    抛 UploadError(人话)或 OSError(网络层)。progress 收到 0..100。
    """
    if len(data) == 0:
        raise UploadError("空文件,没东西可传")
    if len(name.encode("utf-8", "replace")) >= 32:
        raise UploadError("名字太长(UTF-8 编码后要小于 32 字节)")
    if len(family.encode("ascii", "replace")) >= 8:
        raise UploadError("家族名要小于 8 个 ASCII 字符")
    if len(data) % 2:
        raise UploadError("镜像大小必须是偶数(目标按 16 位半字编程)")

    crc = zlib.crc32(data) & 0xFFFFFFFF
    begin = _BEGIN.pack(slot, name.encode("utf-8", "replace"), len(data), crc,
                        family.encode("ascii"), addr)

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _send_frame(sock, FT_BEGIN, begin)
        ftype, payload = _recv_frame(sock)
        if ftype != FT_ACK or len(payload) != 5 or payload[4] != ST_OK:
            raise UploadError("板上拒绝了上传:%s"
                              % (_explain(ftype, payload)))
        if progress:
            progress(0)

        for seq, off in enumerate(range(0, len(data), CHUNK)):
            block = data[off:off + CHUNK]
            _send_frame(sock, FT_CHUNK, _U32.pack(seq) + block)
            ftype, payload = _recv_frame(sock)
            if ftype != FT_ACK or len(payload) != 5 or payload[4] != ST_OK:
                raise UploadError("第 %d 块被拒:%s"
                                  % (seq, _explain(ftype, payload)))
            if progress:
                # 上传占全程的大头,进度就按字节走
                progress(min(99, (off + len(block)) * 100 // len(data)))

        _send_frame(sock, FT_FINISH)
        ftype, payload = _recv_frame(sock)
        if ftype != FT_RESULT or not payload:
            raise UploadError("收尾应答不是 RESULT:%s" % _explain(ftype, payload))
        status, msg = payload[0], payload[1:].split(b"\x00", 1)[0].decode(
            "utf-8", "replace")
        if status != ST_OK:
            raise UploadError(_STATUS_TEXT.get(status, "上传失败(%d)" % status),
                              status)
        if progress:
            progress(100)
        return msg or "saved"


def _explain(ftype: int, payload: bytes) -> str:
    if ftype == FT_RESULT and payload:
        status = payload[0]
        return _STATUS_TEXT.get(status, "status=%d" % status)
    if ftype == FT_ACK and len(payload) == 5:
        status = payload[4]
        if status == ST_OK:
            return "意外的 ACK"
        return _STATUS_TEXT.get(status, "status=%d" % status)
    return "帧类型 0x%02x 不是预期的应答" % ftype
