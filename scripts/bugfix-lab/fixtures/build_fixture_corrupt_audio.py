"""Corrupt every audio SAMPLE BYTE of an mp4 in place (structure/offsets/length
untouched) so ffprobe -show_format -show_streams still reports the audio
track normally, but ffmpeg's actual decode of every audio frame fails
(garbage AAC bitstream), producing a genuine nonzero process exit.
"""
import struct
import sys


def read_boxes(data, start, end):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos+4])[0]
        typ = data[pos+4:pos+8].decode("latin1")
        if size == 1:
            largesize = struct.unpack(">Q", data[pos+8:pos+16])[0]
            box_end = pos + largesize
        elif size == 0:
            box_end = end
        else:
            box_end = pos + size
        yield typ, pos, box_end
        pos = box_end


def children(data, box_start, box_end):
    """Children of the box whose OWN header spans [box_start, box_start+8ish)."""
    return list(read_boxes(data, box_start + 8, box_end))


def child(data, box_start, box_end, name):
    for typ, bs, be in children(data, box_start, box_end):
        if typ == name:
            return bs, be
    return None


def main():
    src, dst = sys.argv[1], sys.argv[2]
    data = bytearray(open(src, "rb").read())
    n = len(data)

    moov = None
    for typ, bs, be in read_boxes(data, 0, n):
        if typ == "moov":
            moov = (bs, be)
    assert moov, "no moov box"

    traks = [(bs, be) for typ, bs, be in children(data, *moov) if typ == "trak"]
    assert traks, "no trak boxes"

    audio_trak = None
    for ts, te in traks:
        mdia = child(data, ts, te, "mdia")
        if not mdia:
            continue
        hdlr = child(data, *mdia, "hdlr")
        if not hdlr:
            continue
        hs, he = hdlr
        handler_off = hs + 8 + 4 + 4
        handler_type = bytes(data[handler_off:handler_off+4]).decode("latin1")
        if handler_type == "soun":
            audio_trak = (ts, te)
            break

    assert audio_trak, "no audio (soun) trak found"
    mdia = child(data, *audio_trak, "mdia")
    minf = child(data, *mdia, "minf")
    stbl = child(data, *minf, "stbl")

    stsz = child(data, *stbl, "stsz")
    stco = child(data, *stbl, "stco")
    co64 = child(data, *stbl, "co64")
    assert stsz, "no stsz"

    stsz_s, stsz_e = stsz
    p = stsz_s + 8 + 4  # skip version/flags
    sample_size = struct.unpack(">I", data[p:p+4])[0]; p += 4
    sample_count = struct.unpack(">I", data[p:p+4])[0]; p += 4
    sizes = []
    if sample_size == 0:
        for i in range(sample_count):
            sz = struct.unpack(">I", data[p:p+4])[0]; p += 4
            sizes.append(sz)
    else:
        sizes = [sample_size] * sample_count

    offsets = []
    if stco:
        cs, ce = stco
        p = cs + 8 + 4  # skip version/flags
        entry_count = struct.unpack(">I", data[p:p+4])[0]; p += 4
        for i in range(entry_count):
            off = struct.unpack(">I", data[p:p+4])[0]; p += 4
            offsets.append(off)
    elif co64:
        cs, ce = co64
        p = cs + 8 + 4  # skip version/flags
        entry_count = struct.unpack(">I", data[p:p+4])[0]; p += 4
        for i in range(entry_count):
            off = struct.unpack(">Q", data[p:p+8])[0]; p += 8
            offsets.append(off)
    else:
        raise AssertionError("no stco/co64")

    stsc = child(data, *stbl, "stsc")
    assert stsc, "no stsc"
    cs, ce = stsc
    p = cs + 8 + 4  # skip version/flags
    stsc_entry_count = struct.unpack(">I", data[p:p+4])[0]; p += 4
    stsc_entries = []  # (first_chunk, samples_per_chunk)
    for i in range(stsc_entry_count):
        first_chunk = struct.unpack(">I", data[p:p+4])[0]; p += 4
        samples_per_chunk = struct.unpack(">I", data[p:p+4])[0]; p += 4
        p += 4  # sample_description_index
        stsc_entries.append((first_chunk, samples_per_chunk))

    def samples_per_chunk_for(chunk_1indexed):
        spc = stsc_entries[0][1]
        for first_chunk, s in stsc_entries:
            if chunk_1indexed >= first_chunk:
                spc = s
            else:
                break
        return spc

    total = 0
    sample_idx = 0
    for chunk_i, chunk_off in enumerate(offsets, start=1):
        spc = samples_per_chunk_for(chunk_i)
        within = 0
        for _ in range(spc):
            if sample_idx >= len(sizes):
                break
            sz = sizes[sample_idx]
            sample_idx += 1
            for i in range(sz):
                data[chunk_off + within + i] = 0xFF
            within += sz
            total += sz
    assert sample_idx == len(sizes), f"consumed {sample_idx} of {len(sizes)} samples — stsc mapping incomplete"

    open(dst, "wb").write(data)
    print(f"corrupted {total} audio sample bytes across {len(sizes)} samples; "
          f"file size unchanged: {len(data)} == {n}: {len(data) == n}")


if __name__ == "__main__":
    main()

