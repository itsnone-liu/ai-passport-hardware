"""让所有脚本打开 COM3 时不再拉 DTR 复位设备。

为什么放在 sitecustomize：这些脚本有一堆共用的打开方式，逐个改总会漏一个，
而漏掉的那个就会让设备重启、让对面看到"control channel closed + 新 hello"，
然后我们花时间排查一个自己制造的故障。这里是介入点最靠前的一次性修正。

只要 PYTHONPATH 里有 D:\dsh-work\tools，Python 启动时就会自动导入本文件。
"""
import serial

_Orig = serial.Serial


class Serial(_Orig):
    def open(self):
        if self.port:
            # 必须在 open() 之前设置：open 时拉 DTR/RTS 就是 ESP32 的复位握手。
            try:
                self.dtr = False
                self.rts = False
            except Exception:
                pass
        super().open()


serial.Serial = Serial