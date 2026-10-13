#!/usr/bin/env python3
"""สร้างคลิปแนวตั้ง 9:16 สำหรับ Shopee Video (และ TikTok) จากรูปสินค้า

ใช้งาน:
    python3 make_clip.py clip.json          # สร้างคลิปตามไฟล์ตั้งค่า
    python3 make_clip.py --demo out_dir     # สร้างคลิปตัวอย่างจากรูปจำลอง
    python3 make_clip.py --from-link URL    # หาลิงก์รูปสินค้าจากลิงก์ affiliate/หน้าสินค้า

ช่อง "image" ใส่ได้ทั้งไฟล์ในเครื่องและลิงก์รูป (https://...) ลิงก์รูปจะถูกดาวน์โหลดให้อัตโนมัติ

ต้องมี: Python 3 + Pillow และ ffmpeg
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FPS = 30
# ด้านล่างและด้านขวาของจอ Shopee Video มีปุ่มตะกร้า/ไลก์ทับอยู่ ข้อความหลักจึงต้องอยู่นอกโซนนี้
SAFE_BOTTOM = 380
SAFE_RIGHT = 150

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/tlwg/Loma-Bold.otf",
    "/usr/share/fonts/truetype/tlwg/Loma-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Thonburi.ttc",
    "C:/Windows/Fonts/tahomabd.ttf",
]

# สระ/วรรณยุกต์ที่ต้องอยู่ติดกับพยัญชนะตัวก่อนหน้า และสระหน้าที่ต้องอยู่ติดกับตัวถัดไป
THAI_FOLLOWERS = set("\u0e30\u0e31\u0e32\u0e33\u0e34\u0e35\u0e36\u0e37\u0e38\u0e39\u0e3a"
                     "\u0e45\u0e46\u0e47\u0e48\u0e49\u0e4a\u0e4b\u0e4c\u0e4d\u0e4e")
THAI_LEADERS = set("\u0e40\u0e41\u0e42\u0e43\u0e44")

COLORS = {
    "orange": (238, 77, 45),
    "white": (255, 255, 255),
    "dark": (20, 20, 20),
    "yellow": (255, 214, 0),
}


def find_font(custom=None):
    for path in [custom] + FONT_CANDIDATES:
        if path and os.path.exists(path):
            return path
    sys.exit("ไม่พบฟอนต์ภาษาไทย ให้ใส่ \"font\": \"path/to/font.ttf\" ในไฟล์ตั้งค่า")


def text_width(draw, text, font):
    return draw.textlength(text, font=font)


def split_units(text):
    """แบ่งข้อความเป็นหน่วยที่ตัดบรรทัดได้ โดยไม่แยกสระ/วรรณยุกต์ออกจากพยัญชนะ"""
    units = []
    for ch in text:
        if units and (ch in THAI_FOLLOWERS or units[-1][-1] in THAI_LEADERS):
            units[-1] += ch
        else:
            units.append(ch)
    return units


def wrap(draw, text, font, max_w):
    """ตัดบรรทัด: ขึ้นบรรทัดใหม่ตาม \\n ก่อน แล้วตัดตามช่องว่าง ถ้ายังยาวเกินจึงตัดตามตัวอักษร"""
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            candidate = f"{line} {word}".strip()
            if text_width(draw, candidate, font) <= max_w:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for unit in split_units(word):
                if text_width(draw, line + unit, font) > max_w and line:
                    lines.append(line)
                    line = ""
                line += unit
        lines.append(line)
    return lines


def block_height(text, font_path, size, max_w=W - 120):
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = wrap(draw, text, ImageFont.truetype(font_path, size), max_w)
    return int(size * 1.45) * len(lines)


def draw_block(img, text, font_path, size, y, fg, bg=None, max_w=W - 120, anchor_x=None):
    """วาดข้อความหลายบรรทัดกึ่งกลาง พร้อมกล่องพื้นหลังโค้งมน คืนค่าตำแหน่ง y ล่างสุด"""
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, size)
    lines = wrap(draw, text, font, max_w)
    line_h = int(size * 1.45)
    cx = anchor_x if anchor_x is not None else W // 2
    if bg:
        widest = max(text_width(draw, ln, font) for ln in lines)
        pad = 28
        draw.rounded_rectangle(
            [cx - widest / 2 - pad, y - pad * 0.6, cx + widest / 2 + pad, y + line_h * len(lines) + pad * 0.4],
            radius=28, fill=bg)
    for i, ln in enumerate(lines):
        draw.text((cx, y + i * line_h), ln, font=font, fill=fg, anchor="ma",
                  stroke_width=0 if bg else 4, stroke_fill=(0, 0, 0))
    return y + line_h * len(lines)


def cover(img, w, h):
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def contain(img, w, h):
    scale = min(w / img.width, h / img.height)
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)


def rounded(img, radius=40):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width, img.height], radius=radius, fill=255)
    out = Image.new("RGBA", img.size)
    out.paste(img, (0, 0), mask)
    return out


def compose_slide(image_path, top_text, font, price=None, cta=None, disclosure=None, big_hook=False):
    product = Image.open(image_path).convert("RGB")
    frame = cover(product, W, H).filter(ImageFilter.GaussianBlur(40))
    frame = Image.blend(frame, Image.new("RGB", (W, H), (0, 0, 0)), 0.45).convert("RGBA")

    # จัดวางจากบน (ข้อความหัว) และจากล่าง (ราคา + CTA) แล้วให้รูปสินค้าอยู่ในพื้นที่ที่เหลือ
    gap = 56
    top_size = 84 if big_hook else 68
    top_end = 150 + block_height(top_text, font, top_size) + gap if top_text else 150
    cta_w = W - SAFE_RIGHT - 120
    cta_x = (W - SAFE_RIGHT) // 2
    stack = []
    if price:
        stack.append((price, 80, COLORS["dark"], COLORS["yellow"]))
    if cta:
        stack.append((cta, 54, COLORS["white"], COLORS["orange"]))
    stack_h = sum(block_height(t, font, s, cta_w) + gap for t, s, _, _ in stack)
    stack_y = H - SAFE_BOTTOM - stack_h

    area_h = stack_y - gap - top_end
    fg = rounded(contain(product, W - 140, area_h))
    frame.alpha_composite(fg, ((W - fg.width) // 2, top_end + (area_h - fg.height) // 2))

    if top_text:
        draw_block(frame, top_text, font, top_size, 150,
                   COLORS["white"], bg=COLORS["orange"] if big_hook else None)
    y = stack_y + gap // 2
    for text, size, fg_color, bg_color in stack:
        y = draw_block(frame, text, font, size, y, fg_color, bg=bg_color, max_w=cta_w, anchor_x=cta_x) + gap
    if disclosure:
        draw_block(frame, disclosure, font, 32, H - SAFE_BOTTOM + 20, (230, 230, 230),
                   max_w=W - SAFE_RIGHT - 80, anchor_x=(W - SAFE_RIGHT) // 2)
    return frame.convert("RGB")


BROWSER_UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")


def curl(url, out=None):
    """ดาวน์โหลดด้วย curl (ใช้ proxy/ใบรับรองของเครื่องได้เลย) คืนค่า bytes ถ้าไม่ระบุ out"""
    cmd = ["curl", "-sSL", "--fail", "--max-time", "30", "-A", BROWSER_UA, url]
    if out:
        cmd += ["-o", out]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ดาวน์โหลดไม่สำเร็จ: {url}\n{result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def find_og_image(link):
    """หาลิงก์รูปสินค้าจาก meta og:image ของหน้าสินค้า (ตามลิงก์ affiliate ที่ redirect ไปหน้าสินค้า)"""
    page = curl(link).decode("utf-8", errors="replace")
    found = []
    for tag in re.findall(r"<meta[^>]+>", page, flags=re.I):
        if re.search(r"(property|name)=[\"']og:image(:url)?[\"']", tag, flags=re.I):
            m = re.search(r"content=[\"']([^\"']+)[\"']", tag, flags=re.I)
            if m and m.group(1) not in found:
                found.append(html.unescape(m.group(1)))
    if not found:
        raise RuntimeError(f"ไม่พบรูปในหน้า {link} (Shopee อาจบล็อกการดึงหน้าเว็บ) ให้ใส่ลิงก์รูปเองแทน")
    return found


def local_image(src, base_dir, work):
    """คืน path รูปในเครื่อง ถ้าเป็นลิงก์จะดาวน์โหลดลงโฟลเดอร์ชั่วคราวก่อน แล้วเช็กว่าเปิดเป็นรูปได้"""
    if not src.startswith(("http://", "https://")):
        return src if os.path.isabs(src) else os.path.join(base_dir, src)
    dest = os.path.join(work, f"img_{abs(hash(src))}")
    if not os.path.exists(dest):
        curl(src, dest)
        try:
            Image.open(dest).verify()
        except Exception:
            raise RuntimeError(f"ไฟล์จากลิงก์นี้ไม่ใช่รูป: {src}")
    return dest


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"ffmpeg ผิดพลาด:\n{result.stderr[-2000:]}")


def build(config, base_dir):
    if not shutil.which("ffmpeg"):
        sys.exit("ไม่พบ ffmpeg ติดตั้งก่อน เช่น `brew install ffmpeg` หรือ `sudo apt install ffmpeg`")
    font = find_font(config.get("font"))
    slides = config["slides"]
    if not slides:
        sys.exit("ต้องมีอย่างน้อย 1 slide")

    def path(p):
        return p if os.path.isabs(p) else os.path.join(base_dir, p)

    output = path(config.get("output", "clip.mp4"))
    work = tempfile.mkdtemp(prefix="clip_")
    parts = []
    try:
        for i, slide in enumerate(slides):
            first, last = i == 0, i == len(slides) - 1
            img = compose_slide(
                local_image(slide["image"], base_dir, work),
                config["hook"] if first and config.get("hook") else slide.get("text", ""),
                font,
                price=config.get("price") if (first or last) else None,
                cta=config.get("cta") if last else None,
                disclosure=config.get("disclosure"),
                big_hook=first and bool(config.get("hook")),
            )
            png = os.path.join(work, f"s{i}.png")
            img.save(png)
            if first:
                img.save(os.path.splitext(output)[0] + "_cover.jpg", quality=92)
            seconds = float(slide.get("seconds", 3 if not first else 3.5))
            frames = int(seconds * FPS)
            mp4 = os.path.join(work, f"s{i}.mp4")
            # ซูมเข้าช้าๆ ให้ภาพนิ่งดูมีการเคลื่อนไหว
            zoom = (f"scale={W * 2}:{H * 2},zoompan=z='min(zoom+0.0006,1.08)':d={frames}"
                    f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},format=yuv420p")
            run(["ffmpeg", "-y", "-loop", "1", "-i", png, "-vf", zoom, "-frames:v", str(frames),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", mp4])
            parts.append(mp4)

        listing = os.path.join(work, "list.txt")
        with open(listing, "w") as f:
            f.writelines(f"file '{p}'\n" for p in parts)
        video = os.path.join(work, "video.mp4")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", video])

        music = config.get("music")
        if music:
            total = sum(float(s.get("seconds", 3.5 if i == 0 else 3)) for i, s in enumerate(slides))
            fade = f"afade=t=out:st={max(total - 1.5, 0):.2f}:d=1.5"
            audio = ["-i", path(music), "-af", fade, "-shortest"]
        else:
            audio = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest"]
        run(["ffmpeg", "-y", "-i", video, *audio, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", output])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"สร้างคลิปแล้ว: {output}")
    print(f"รูปปก: {os.path.splitext(output)[0]}_cover.jpg")


def make_demo(out_dir):
    """สร้างรูปสินค้าจำลอง 3 รูป แล้วสร้างคลิปตัวอย่าง"""
    os.makedirs(out_dir, exist_ok=True)
    palette = [((255, 236, 210), (238, 77, 45)), ((220, 240, 255), (40, 120, 200)),
               ((225, 250, 225), (40, 160, 90))]
    names = []
    for i, (bg, fg) in enumerate(palette):
        img = Image.new("RGB", (900, 900), bg)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([220, 200, 680, 760], radius=60, fill=fg)
        d.ellipse([360, 120, 540, 300], fill=fg)
        name = os.path.join(out_dir, f"demo_product{i + 1}.png")
        img.save(name)
        names.append(os.path.basename(name))
    config = {
        "output": "demo_clip.mp4",
        "hook": "อย่าเพิ่งเลื่อนผ่าน!\nของดีไม่ถึง 100 บาท",
        "price": "เพียง 89.-",
        "cta": "กดตะกร้าด้านล่างได้เลย",
        "disclosure": "ลิงก์ในตะกร้าเป็นลิงก์ affiliate",
        "slides": [
            {"image": names[0], "seconds": 3.5},
            {"image": names[1], "text": "ใช้ง่าย ล้างง่าย", "seconds": 3},
            {"image": names[2], "text": "รีวิว 4.9 ดาว\nขายแล้ว 10,000+ ชิ้น", "seconds": 3.5},
        ],
    }
    with open(os.path.join(out_dir, "demo_clip.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    build(config, out_dir)


def main():
    parser = argparse.ArgumentParser(description="สร้างคลิป Shopee Video 9:16 จากรูปสินค้า")
    parser.add_argument("config", nargs="?", help="ไฟล์ตั้งค่า .json")
    parser.add_argument("--demo", metavar="OUT_DIR", help="สร้างคลิปตัวอย่างลงโฟลเดอร์นี้")
    parser.add_argument("--from-link", metavar="URL", help="หาลิงก์รูปสินค้าจากลิงก์ affiliate")
    args = parser.parse_args()
    try:
        if args.from_link:
            print("\n".join(find_og_image(args.from_link)))
            return
    except RuntimeError as e:
        sys.exit(str(e))
    if args.demo:
        make_demo(args.demo)
    elif args.config:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        try:
            build(config, os.path.dirname(os.path.abspath(args.config)))
        except RuntimeError as e:
            sys.exit(str(e))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
