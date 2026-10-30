"""assets/icon.ico 생성 (Pillow 필요: pip install pillow)."""
import os

from PIL import Image, ImageDraw

S = 256
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([16, 16, 240, 240], radius=44, fill=(47, 111, 228, 255))
for y, c in [(70, (255, 255, 255, 255)), (110, (220, 230, 255, 255)), (150, (190, 205, 250, 255))]:
    d.rounded_rectangle([58, y, 198, y + 34], radius=6, fill=c)
    d.rectangle([66, y + 6, 78, y + 28], fill=(47, 111, 228, 255))
for k in range(5):
    d.rectangle([120 + (k % 2) * 10, 76 + k * 22, 132 + (k % 2) * 10, 86 + k * 22], fill=(30, 32, 38, 255))
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icon.ico")
os.makedirs(os.path.dirname(out), exist_ok=True)
im.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon:", out)
