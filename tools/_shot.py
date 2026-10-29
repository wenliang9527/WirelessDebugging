"""抓 tkinter 窗口截图(PrintWindow,不受遮挡影响)。

用法:python _shot.py out.png [--tab N] [--select ssid]
需要窗口已经打开。仅开发调试用,不随工具发布。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 0x00000002


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def find_window(substr="配网工具"):
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, lp):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, b, n + 1)
                if substr in b.value:
                    r = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    found.append((hwnd, r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(proc(cb), 0)
    return found[0] if found else None


def capture(hwnd, l, t, r, b, path):
    w, h = r - l, b - t
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbm = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbm)
    user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(path)

    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    return (w, h)


def click(hwnd, x, y):
    """在窗口客户区坐标点击(用于切 tab)。"""
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    user32.SetCursorPos(x, y)
    time.sleep(0.1)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
    time.sleep(0.6)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
    win = find_window()
    if not win:
        print("窗口没找到")
        sys.exit(1)
    hwnd, l, t, r, b = win
    if "--tab" in sys.argv:
        idx = int(sys.argv[sys.argv.index("--tab") + 1])
        # 三个 tab 的标签大致位置(窗口左上角起算)
        x = l + 40 + idx * 96
        click(hwnd, x, t + 152)
    print(capture(hwnd, l, t, r, b, out))
