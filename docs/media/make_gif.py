#!/usr/bin/env python3
"""Builds an animated GIF from PNG screenshots with the Python standard library only (no Pillow, no ffmpeg).

    python docs/media/make_gif.py out.gif frame1.png frame2.png ... [--delay-ms 1100] [--hold-first 2] [--hold-last 3]

Reads 8-bit RGB or RGBA non-interlaced PNGs (what Playwright writes), builds one global palette (median cut over the colours of every
frame), keeps only the changed rectangle of each frame after the first (unchanged pixels are transparent) and writes the LZW data itself.
"""
import struct
import sys
import zlib

PALETTE_SIZE = 255  # one more index is the transparent colour
TRANSPARENT = 255


def read_png(path):
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s is not a PNG" % path)
    position, chunks = 8, []
    header = None
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        body = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            chunks.append(body)
    width, height, depth, color_type, _, _, interlace = header
    if depth != 8 or color_type not in (2, 6) or interlace:
        raise ValueError("%s: only 8-bit RGB/RGBA PNGs without interlace are supported" % path)
    pixel = 3 if color_type == 2 else 4
    raw = zlib.decompress(b"".join(chunks))
    stride = width * pixel
    rows, previous = [], bytearray(stride)
    offset = 0
    for _ in range(height):
        kind = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        if kind == 1:
            for i in range(pixel, stride):
                line[i] = (line[i] + line[i - pixel]) & 255
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 255
        elif kind == 3:
            for i in range(stride):
                left = line[i - pixel] if i >= pixel else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                a = line[i - pixel] if i >= pixel else 0
                b = previous[i]
                c = previous[i - pixel] if i >= pixel else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        previous = line
    if pixel == 4:
        rows = [bytearray(b for index, b in enumerate(row) if index % 4 != 3) for row in rows]
    return width, height, rows


def colour_counts(rows, counts):
    for row in rows:
        for i in range(0, len(row), 3 * 2):  # every second pixel is enough to find the palette
            key = ((row[i] >> 3) << 10) | ((row[i + 1] >> 3) << 5) | (row[i + 2] >> 3)
            counts[key] = counts.get(key, 0) + 1


def median_cut(counts, size):
    boxes = [list(counts.items())]
    while len(boxes) < size:
        boxes.sort(key=lambda box: -len(box))
        box = boxes.pop(0)
        if len(box) < 2:
            boxes.append(box)
            break
        ranges = []
        for channel in range(3):
            shift = 10 - 5 * channel
            values = [(key >> shift) & 31 for key, _ in box]
            ranges.append(max(values) - min(values))
        channel = ranges.index(max(ranges))
        shift = 10 - 5 * channel
        box.sort(key=lambda item: (item[0] >> shift) & 31)
        total, running, cut = sum(weight for _, weight in box), 0, 1
        for index, (_, weight) in enumerate(box):
            running += weight
            if running * 2 >= total:
                cut = max(1, min(index + 1, len(box) - 1))
                break
        boxes += [box[:cut], box[cut:]]
    palette = []
    for box in boxes:
        weight = sum(w for _, w in box) or 1
        red = sum(((k >> 10) & 31) * w for k, w in box) / weight
        green = sum(((k >> 5) & 31) * w for k, w in box) / weight
        blue = sum((k & 31) * w for k, w in box) / weight
        palette.append((int(red * 8 + 4), int(green * 8 + 4), int(blue * 8 + 4)))
    return palette


def index_frame(rows, palette, cache):
    out = []
    for row in rows:
        line = bytearray(len(row) // 3)
        for x in range(len(line)):
            i = 3 * x
            colour = (row[i], row[i + 1], row[i + 2])
            index = cache.get(colour)
            if index is None:
                index = min(range(len(palette)), key=lambda n: (palette[n][0] - colour[0]) ** 2 + (palette[n][1] - colour[1]) ** 2 + (palette[n][2] - colour[2]) ** 2)
                cache[colour] = index
            line[x] = index
        out.append(line)
    return out


def lzw(indices, min_code=8):
    clear, end = 1 << min_code, (1 << min_code) + 1
    table, next_code, width = {}, end + 1, min_code + 1
    output, bits, bit_count = bytearray(), 0, 0

    def emit(code):
        nonlocal bits, bit_count
        bits |= code << bit_count
        bit_count += width
        while bit_count >= 8:
            output.append(bits & 255)
            bits >>= 8
            bit_count -= 8

    emit(clear)
    prefix = indices[0]
    for symbol in indices[1:]:
        key = (prefix << 8) | symbol
        found = table.get(key)
        if found is not None:
            prefix = found
            continue
        emit(prefix)
        if next_code < 4096:
            table[key] = next_code
            next_code += 1
            if next_code > (1 << width) and width < 12:
                width += 1
        else:
            emit(clear)
            table.clear()
            next_code, width = end + 1, min_code + 1
        prefix = symbol
    emit(prefix)
    emit(end)
    if bit_count:
        output.append(bits & 255)
    return bytes(output)


def blocks(data):
    out = bytearray()
    for start in range(0, len(data), 255):
        chunk = data[start:start + 255]
        out.append(len(chunk))
        out += chunk
    return bytes(out) + b"\x00"


def changed_box(previous, current):
    top = left = None
    bottom = right = -1
    for y, (a, b) in enumerate(zip(previous, current)):
        if a == b:
            continue
        xs = [x for x in range(len(a)) if a[x] != b[x]]
        top = y if top is None else top
        bottom = y
        left = xs[0] if left is None else min(left, xs[0])
        right = max(right, xs[-1])
    return None if top is None else (left, top, right, bottom)


def build(paths, out_path, delay_ms=1100, hold_first=2, hold_last=3):
    counts = {}
    frames = []
    for path in paths:
        width, height, rows = read_png(path)
        colour_counts(rows, counts)
        frames.append(rows)
        print("read", path, file=sys.stderr)
    palette = median_cut(counts, PALETTE_SIZE)
    while len(palette) < 256:
        palette.append((0, 0, 0))
    cache = {}
    indexed = [index_frame(rows, palette[:PALETTE_SIZE], cache) for rows in frames]
    out = bytearray(b"GIF89a" + struct.pack("<HHBBB", width, height, 0xF7, 0, 0))
    for red, green, blue in palette:
        out += bytes((red, green, blue))
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"
    previous = None
    for number, frame in enumerate(indexed):
        delay = delay_ms * (hold_first if number == 0 else hold_last if number == len(indexed) - 1 else 1)
        if previous is None:
            box, source, transparent = (0, 0, width - 1, height - 1), frame, 0
        else:
            box = changed_box(previous, frame)
            if box is None:
                box = (0, 0, 0, 0)
            left, top, right, bottom = box
            source = []
            for y in range(top, bottom + 1):
                line = bytearray(frame[y][left:right + 1])
                before = previous[y][left:right + 1]
                for x in range(len(line)):
                    if line[x] == before[x]:
                        line[x] = TRANSPARENT
                source.append(line)
            transparent = 1
        left, top, right, bottom = box
        if previous is not None:
            source = [bytearray(row) for row in source]
        out += b"\x21\xF9\x04" + bytes((0x04 | transparent,)) + struct.pack("<H", max(2, delay // 10)) + bytes((TRANSPARENT, 0))
        out += b"\x2C" + struct.pack("<HHHHB", left, top, right - left + 1, bottom - top + 1, 0)
        out += bytes((8,)) + blocks(lzw(b"".join(bytes(row) for row in source)))
        previous = frame
        print("encoded frame %d/%d (%d bytes so far)" % (number + 1, len(indexed), len(out)), file=sys.stderr)
    out += b"\x3B"
    open(out_path, "wb").write(out)
    return len(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    options = {"--delay-ms": 1100, "--hold-first": 2, "--hold-last": 3}
    files = []
    while args:
        item = args.pop(0)
        if item in options:
            options[item] = int(args.pop(0))
        else:
            files.append(item)
    size = build(files[1:], files[0], options["--delay-ms"], options["--hold-first"], options["--hold-last"])
    print("%s: %d bytes" % (files[0], size))
