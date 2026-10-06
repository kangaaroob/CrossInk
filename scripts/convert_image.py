#!/usr/bin/env python3
"""
Convert images to/from the 1-bpp C arrays used by GfxRenderer::drawImage.

The firmware stores bitmaps as packed monochrome: row-major, MSB first, each
row padded to a byte boundary, and a set bit means WHITE (background). That is
the same layout scripts/convert_icon.py emits, and the layout GfxRenderer and
the panel driver expect.

Two subcommands:

  encode  PNG/SVG -> C header (and optionally a preview PNG of the result)
  decode  C header -> PNG, so you can see exactly what the device will draw

Encoding is lossy (colour and resolution are thrown away), so `encode` always
supports --preview and `decode` exists to inspect any header already in tree.

Usage:
    python scripts/convert_image.py encode logo.png -o src/images/Logo120.h \\
        --name Logo120 --size 120x120 --preview /tmp/logo-preview.png

    python scripts/convert_image.py decode src/images/Logo120.h \\
        -o /tmp/current-logo.png --scale 4

Notes:
    Bitmaps are stored pre-rotated 90 degrees counter-clockwise, because
    GfxRenderer::drawImage rotates the origin corner but not the bitmap
    contents (see the "TODO: Rotate bits" in lib/GfxRenderer/GfxRenderer.cpp).
    Both scripts/convert_icon.py and src/images/Logo120.h follow that
    convention, so --rotate defaults to 90 and art that looks upright in your
    editor will look upright on the panel. Pass --rotate 0 only if you are
    hand-authoring bits already in panel space; decode an existing header first
    if you are unsure.
"""

import argparse
import os
import re
import sys
from typing import List, Optional, Tuple

# A set bit is white. Grayscale values at or above this become white.
DEFAULT_THRESHOLD = 128

# 4-space indent + 19 * len("0xff, ") - 1 == 118, just inside the repo's
# 120-column clang-format limit, so generated headers survive clang-format
# without reflowing.
BYTES_PER_LINE = 19


def _resampling_filter():
    """Pillow moved the resampling enum in 9.1; support old and new."""
    from PIL import Image

    return getattr(Image, "Resampling", Image).LANCZOS


def parse_size(text: str) -> Tuple[int, int]:
    """Parse a WxH size string such as '120x120'."""
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError(f"Expected WxH (e.g. 120x120), got: {text}")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(f"Size must be positive, got: {text}")
    return width, height


def row_stride(width: int) -> int:
    """Bytes per row; each row is padded out to a whole byte."""
    return (width + 7) // 8


# --------------------------------------------------------------------------
# encode
# --------------------------------------------------------------------------


def svg_to_png_bytes(svg_path: str, width: int, height: int) -> bytes:
    import cairosvg

    with open(svg_path, "rb") as f:
        svg_data = f.read()
    return cairosvg.svg2png(bytestring=svg_data, output_width=width, output_height=height)


def _downscale(img, size, resample: str):
    """
    Shrink a grayscale image, optionally protecting thin dark lines.

    Averaging filters (lanczos, box) blend a 1px black line with its white
    neighbours, so at large reductions the line lands near mid-gray and the
    threshold drops it in and out - that is what makes hairlines look dashed.
    'min' takes the darkest pixel in each source neighbourhood instead, so any
    line that exists at all survives at full strength.
    """
    from PIL import Image, ImageFilter

    if resample == "min":
        source_width, source_height = img.size
        ratio = max(source_width / size[0], source_height / size[1])
        # Kernel wide enough to cover one destination pixel's footprint, odd for PIL.
        kernel = max(3, int(ratio) | 1)
        img = img.filter(ImageFilter.MinFilter(kernel))
        return img.resize(size, Image.BOX)

    filters = {"lanczos": _resampling_filter(), "box": Image.BOX}
    return img.resize(size, filters[resample])


def load_image(
    path: str,
    width: int,
    height: int,
    fit: str,
    background: str,
    rotate: int,
    resample: str = "lanczos",
    thicken: int = 0,
    autocrop: bool = False,
    autocrop_tolerance: int = 24,
):
    """Load any supported image and return a grayscale image of exactly width x height."""
    import io

    from PIL import Image, ImageFilter

    pad = (255, 255, 255) if background == "white" else (0, 0, 0)

    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg":
        img = Image.open(io.BytesIO(svg_to_png_bytes(path, width, height))).convert("RGBA")
    else:
        img = Image.open(path).convert("RGBA")

    # Flatten transparency onto the pad colour before scaling, so semi
    # transparent edges resolve against the same background they land on.
    flattened = Image.new("RGBA", img.size, pad + (255,))
    flattened.paste(img, mask=img.split()[3])
    img = flattened.convert("L")

    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor=pad[0])

    # Trim uniform background borders so the art fills the target box instead
    # of inheriting whatever margin the source file happened to have.
    if autocrop:
        from PIL import ImageChops

        # Compare against the corner pixel, which is background by definition.
        reference = Image.new("L", img.size, img.getpixel((0, 0)))
        bbox = ImageChops.difference(img, reference).point(lambda v: 255 if v > autocrop_tolerance else 0).getbbox()
        if bbox:
            img = img.crop(bbox)

    # Fatten strokes at source resolution so they still exist after shrinking.
    if thicken:
        img = img.filter(ImageFilter.MinFilter(2 * thicken + 1))

    source_width, source_height = img.size
    if fit == "stretch":
        return _downscale(img, (width, height), resample)

    scale_fn = min if fit == "contain" else max
    scale = scale_fn(width / source_width, height / source_height)
    scaled_size = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
    img = _downscale(img, scaled_size, resample)

    # "contain" pads out to size; "cover" overflows and the paste crops it.
    canvas = Image.new("L", (width, height), pad[0])
    canvas.paste(img, ((width - img.size[0]) // 2, (height - img.size[1]) // 2))
    return canvas


def binarize(img, threshold: int, dither: bool, invert: bool):
    """Reduce to pure black/white. Returns an 'L' image containing only 0 and 255."""
    from PIL import Image

    gray = img.convert("L")
    if dither:
        # Floyd-Steinberg. Better for photos and gradients, noisier for line art.
        mono = gray.convert("1")
    else:
        mono = gray.point(lambda v: 255 if v >= threshold else 0, mode="1")
    out = mono.convert("L")
    if invert:
        out = Image.eval(out, lambda v: 255 - v)
    return out


def pack_bits(img) -> bytearray:
    """Pack an 'L' image of 0/255 pixels into row-major, MSB-first, 1=white bytes."""
    width, height = img.size
    pixels = img.load()
    packed = bytearray()
    for y in range(height):
        for x_base in range(0, width, 8):
            byte = 0
            for bit in range(8):
                x = x_base + bit
                # Bits past the right edge stay 1 (white) so padding blends in.
                is_white = pixels[x, y] >= DEFAULT_THRESHOLD if x < width else True
                if is_white:
                    byte |= 1 << (7 - bit)
            packed.append(byte)
    return packed


def format_header(packed: bytes, name: str, width: int, height: int, source: str, defines: bool) -> str:
    expected = row_stride(width) * height
    lines: List[str] = [
        "#pragma once",
        "#include <cstdint>",
        "",
        f"// '{name}', {width}x{height}px",
        f"// Generated by scripts/convert_image.py from {os.path.basename(source)}",
    ]
    if defines:
        macro = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
        lines += ["", f"#define {macro}_WIDTH {width}", f"#define {macro}_HEIGHT {height}"]
    lines += ["", f"static const uint8_t {name}[] = {{"]

    for start in range(0, len(packed), BYTES_PER_LINE):
        chunk = packed[start : start + BYTES_PER_LINE]
        row = ", ".join(f"0x{value:02x}" for value in chunk)
        terminator = "," if start + BYTES_PER_LINE < len(packed) else ""
        lines.append(f"    {row}{terminator}")

    lines += [
        "};",
        "",
        f'static_assert(sizeof({name}) == {expected}, "{name} must be exactly {width}x{height} / 8 bytes");',
        "",
    ]
    return "\n".join(lines)


def default_array_name(output_path: str) -> str:
    stem = os.path.splitext(os.path.basename(output_path))[0]
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", stem)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"Image{cleaned}"
    return cleaned[0].upper() + cleaned[1:]


def cmd_encode(args: argparse.Namespace) -> int:
    width, height = args.size
    img = load_image(
        args.input,
        width,
        height,
        args.fit,
        args.background,
        args.rotate,
        args.resample,
        args.thicken,
        args.autocrop,
        args.autocrop_tolerance,
    )
    mono = binarize(img, args.threshold, args.dither, args.invert)
    packed = pack_bits(mono)

    expected = row_stride(width) * height
    if len(packed) != expected:
        print(f"Error: packed {len(packed)} bytes but expected {expected}", file=sys.stderr)
        return 1

    name = args.name or default_array_name(args.output)
    header = format_header(packed, name, width, height, args.input, not args.no_defines)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        f.write(header)
    print(f"Wrote {args.output} ({name}, {width}x{height}, {len(packed)} bytes in flash)")

    if args.preview:
        # Undo the pre-rotation so the preview matches what a reader sees.
        render_preview(packed, width, height, args.preview, args.scale, args.show, (360 - args.rotate) % 360)
    return 0


# --------------------------------------------------------------------------
# decode
# --------------------------------------------------------------------------


def parse_c_array(text: str, name: Optional[str]) -> Tuple[str, bytes]:
    """Pull the first (or named) uint8_t array out of a C header."""
    pattern = (
        rf"static\s+const\s+uint8_t\s+({re.escape(name) if name else r'\w+'})\s*\[\s*\]\s*=\s*\{{(.*?)\}}\s*;"
    )
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        target = f" named {name}" if name else ""
        raise ValueError(f"No 'static const uint8_t' array{target} found")
    found_name, body = match.group(1), match.group(2)
    values = [int(token, 16) for token in re.findall(r"0[xX]([0-9a-fA-F]{1,2})\b", body)]
    if not values:
        raise ValueError(f"Array {found_name} contained no hex byte literals")
    return found_name, bytes(values)


def infer_size(text: str, name: str, byte_count: int) -> Optional[Tuple[int, int]]:
    """Recover dimensions from the header's macros or comments, else from a square guess."""
    macro = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    width_match = re.search(rf"#define\s+{macro}_WIDTH\s+(\d+)", text)
    height_match = re.search(rf"#define\s+{macro}_HEIGHT\s+(\d+)", text)
    if width_match and height_match:
        return int(width_match.group(1)), int(height_match.group(1))

    # Comment styles in tree: "// 'crossink', 120x120px" and "// Image dimensions: 48x48".
    # Restricted to comment lines so hex literals like "0x11, 0x22" can't match.
    for comment in re.findall(r"//[^\n]*", text):
        found = re.search(r"(\d+)\s*[xX]\s*(\d+)", comment)
        if found:
            width, height = int(found.group(1)), int(found.group(2))
            if row_stride(width) * height == byte_count:
                return width, height

    # Last resort: assume square, which covers every bitmap currently in tree.
    for side in range(1, byte_count + 1):
        if row_stride(side) * side == byte_count:
            return side, side
        if row_stride(side) * side > byte_count:
            break
    return None


def render_preview(packed: bytes, width: int, height: int, output: str, scale: int, show: bool, rotate: int) -> None:
    """
    Write the packed bits back out as a PNG.

    Stored bits are in panel space (pre-rotated), so a non-zero rotate undoes
    that and yields the orientation a reader actually sees on the device.
    """
    from PIL import Image

    img = Image.new("L", (width, height))
    pixels = img.load()
    stride = row_stride(width)
    for y in range(height):
        for x in range(width):
            byte = packed[y * stride + (x >> 3)]
            bit = (byte >> (7 - (x & 7))) & 1
            pixels[x, y] = 255 if bit else 0

    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor=255)

    if scale > 1:
        img = img.resize((img.size[0] * scale, img.size[1] * scale), Image.NEAREST)

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    img.save(output)
    print(f"Wrote {output} ({img.size[0]}x{img.size[1]} preview)")
    if show:
        img.show()


def cmd_decode(args: argparse.Namespace) -> int:
    with open(args.input) as f:
        text = f.read()

    try:
        name, packed = parse_c_array(text, args.name)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    size = args.size or infer_size(text, name, len(packed))
    if not size:
        print(
            f"Error: could not infer dimensions for {name} ({len(packed)} bytes). Pass --size WxH.",
            file=sys.stderr,
        )
        return 1

    width, height = size
    expected = row_stride(width) * height
    if expected != len(packed):
        print(
            f"Error: {name} has {len(packed)} bytes, but {width}x{height} needs {expected}",
            file=sys.stderr,
        )
        return 1

    orientation = "as stored (panel space)" if args.rotate == 0 else "as drawn on screen"
    print(f"Decoding {name}: {width}x{height}, {len(packed)} bytes, {orientation}")
    render_preview(packed, width, height, args.output, args.scale, args.show, args.rotate)
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert images to/from the firmware's 1-bpp C bitmap arrays.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/convert_image.py encode logo.png -o src/images/Logo120.h \\\n"
            "      --name Logo120 --size 120x120 --preview /tmp/logo.png --scale 4\n"
            "  python scripts/convert_image.py decode src/images/Logo120.h -o /tmp/logo.png --scale 4\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    encode = sub.add_parser("encode", help="PNG/SVG -> C header")
    encode.add_argument("input", help="Source image (.png, .jpg, .svg, ...)")
    encode.add_argument("-o", "--output", required=True, help="Header path to write")
    encode.add_argument("--size", required=True, type=parse_size, help="Target size, e.g. 120x120")
    encode.add_argument("--name", help="C array name (default: derived from the output filename)")
    encode.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"Grayscale cutoff for white, 0-255 (default: {DEFAULT_THRESHOLD})",
    )
    encode.add_argument("--dither", action="store_true", help="Floyd-Steinberg dithering instead of a hard cutoff")
    encode.add_argument("--invert", action="store_true", help="Swap black and white")
    encode.add_argument(
        "--fit",
        choices=("contain", "cover", "stretch"),
        default="contain",
        help="How to reconcile aspect ratios (default: contain, pads to size)",
    )
    encode.add_argument(
        "--background",
        choices=("white", "black"),
        default="white",
        help="Colour behind transparency and padding (default: white)",
    )
    encode.add_argument(
        "--rotate",
        type=int,
        choices=(0, 90, 180, 270),
        default=90,
        help="Counter-clockwise pre-rotation (default: 90, the panel's convention)",
    )
    encode.add_argument(
        "--resample",
        choices=("lanczos", "box", "min"),
        default="lanczos",
        help="Downscale filter (default: lanczos; 'min' keeps thin dark lines intact)",
    )
    encode.add_argument(
        "--thicken",
        type=int,
        default=0,
        help="Fatten dark strokes by N pixels before downscaling (default: 0)",
    )
    encode.add_argument(
        "--autocrop",
        action="store_true",
        help="Trim uniform background borders so the art fills the target box",
    )
    encode.add_argument(
        "--autocrop-tolerance",
        type=int,
        default=24,
        help="How far from the corner colour still counts as background (default: 24)",
    )
    encode.add_argument("--no-defines", action="store_true", help="Omit the _WIDTH/_HEIGHT macros")
    encode.add_argument("--preview", help="Also write a PNG of the encoded result")
    encode.add_argument("--scale", type=int, default=1, help="Nearest-neighbour upscale for the preview")
    encode.add_argument("--show", action="store_true", help="Open the preview in the default image viewer")
    encode.set_defaults(func=cmd_encode)

    decode = sub.add_parser("decode", help="C header -> PNG")
    decode.add_argument("input", help="Header containing a static const uint8_t array")
    decode.add_argument("-o", "--output", required=True, help="PNG path to write")
    decode.add_argument("--name", help="Array to decode (default: the first one found)")
    decode.add_argument("--size", type=parse_size, help="Override dimensions if they can't be inferred")
    decode.add_argument(
        "--rotate",
        type=int,
        choices=(0, 90, 180, 270),
        default=270,
        help="Counter-clockwise correction (default: 270, undoes the stored pre-rotation; 0 shows raw bits)",
    )
    decode.add_argument("--scale", type=int, default=1, help="Nearest-neighbour upscale for readability")
    decode.add_argument("--show", action="store_true", help="Open the PNG in the default image viewer")
    decode.set_defaults(func=cmd_decode)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except FileNotFoundError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except ImportError as err:
        print(f"Error: missing dependency ({err}). Try: pip install -r scripts/requirements.txt", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
