# -*- coding: utf-8 -*-
"""生成应用图标（蓝色圆角底 + 白色"救"字）。"""
import os
from PIL import Image, ImageDraw, ImageFont

SIZE = 256
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角矩形背景
bg = (22, 104, 220, 255)  # #1668DC
d.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=52, fill=bg)

# 顶部"手机"示意：白色圆角条 + 听筒
phone_w, phone_h = 96, 160
px = (SIZE - phone_w) // 2
py = 40
d.rounded_rectangle([px, py, px + phone_w, py + phone_h], radius=18, fill=(255, 255, 255, 255))
d.rounded_rectangle([px + 26, py + 10, px + phone_w - 26, py + 22], radius=6, fill=bg)
d.rounded_rectangle([px + 32, py + phone_h - 18, px + phone_w - 32, py + phone_h - 10],
                    radius=4, fill=bg)

# 底部"救"字
font = None
for cand in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
             r"C:\Windows\Fonts\simhei.ttf"):
    try:
        font = ImageFont.truetype(cand, 84)
        break
    except OSError:
        continue
if font is not None:
    d.text((SIZE // 2, SIZE - 78), "救", font=font, fill=(255, 255, 255, 255),
           anchor="mm", stroke_width=0)

img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon saved:", OUT)
