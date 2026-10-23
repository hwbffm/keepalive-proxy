# Erzeugt keepalive_proxy.ico (grünes Quadrat mit "K", wie das Tray-Icon) für die .exe.
# Public Domain (Unlicense).
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=32, fill=(0, 160, 60))
try:
    font = ImageFont.truetype("arialbd.ttf", 190)
except OSError:
    font = ImageFont.load_default()
d.text((SIZE // 2, SIZE // 2 + 6), "K", fill="white", font=font, anchor="mm")
out = Path(__file__).resolve().parent / "keepalive_proxy.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("geschrieben:", out)
