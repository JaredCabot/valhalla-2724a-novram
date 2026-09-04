#!/usr/bin/env python3
"""Valhalla 2724A NOVRAM tool -- read, decode and verify IC522 / IC523.

The two X2212 devices share one address space at $A000.  IC522 supplies data
bits 0-3 and holds the CALIBRATION block; IC523 supplies bits 4-7 and holds the
USER block.  In each device a byte is stored as two consecutive locations, low
nibble first.  This is exactly what the firmware's store routine at $E0FA and
recall routine at $E165 do.

    calibration   RAM $C300-$C359   90 bytes  -> 180 nibbles in IC522
       data       RAM $C300-$C357   88 bytes
       checksum   RAM $C358-$C359    2 bytes  (16-bit sum of the data words)
    user          RAM $C35A-$C3AD   84 bytes  -> 168 nibbles in IC523
       data       RAM $C35A-$C3AB   82 bytes
       checksum   RAM $C3AC-$C3AD    2 bytes

One file per device, 256 bytes, one byte per nibble. That is the only format;
there is nothing to convert between.

Usage
    novram.py read  PORT --dev cal  -o ic522.bin       read IC522 via the Arduino
    novram.py read  PORT --dev user -o ic523.bin       read IC523
    novram.py check ic522.bin ic523.bin                decode and verify
    novram.py write PORT --dev cal --image ic522.bin   restore a saved image

`write` without --commit is a dry run: it loads the device's static RAM and
verifies it, but never touches the EEPROM array, then issues an array recall so
the part is left exactly as found.  With --commit it additionally commits the
RAM to the EEPROM array and proves the result by recalling and re-reading.
"""
import argparse
import os
import signal
import sys
import time

try:                       # tolerate being piped into head
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

CAL_BASE, CAL_LEN, CAL_SUM = 0xC300, 88, 0xC358      # 90 bytes total
USR_BASE, USR_LEN, USR_SUM = 0xC35A, 82, 0xC3AC      # 84 bytes total
CAL_BYTES, USR_BYTES = 90, 84

# --------------------------------------------------------- calibration map --
# Structure of the 88 calibration bytes, read out of the firmware:
#
#   $C300-$C326   13 constants of 3 bytes each, signed 24-bit big-endian,
#                 fixed point with 1.0 == $400000 (Q22).  The sign is the top
#                 bit of the first byte -- SUB_E1EE tests it with TST ,U / BPL.
#   $C327-$C356   6 records of 8 bytes.  SUB_E3FE selects one with
#                 "LDA #$08 / MUL / LDX #$C327 / ABX" after clamping the index
#                 to 5, and the record is the same 8-byte shape as a user
#                 memory at $C35C.
#   $C357         1 byte.  Inside the checksum, but NOT written by the
#                 default-loading routine at L_E1B4, which copies 87 bytes
#                 ($C300-$C356) and stops.  Purpose undetermined.
#   $C358-$C359   the 16-bit checksum.
#
# SUB_E0A1 -- the routine that prints " CAL DATA OK   " -- walks the 13
# constants in steps of 3 up to $C327 and tests each against a pair of ROM
# tables: nominal values at $EECE and acceptance limits 89 bytes further on at
# $EF27.  It computes |value - nominal|, subtracts the limit and requires the
# result to be negative.  Both tables are reproduced below, verified against
# 2724ACPR.bin.  Every nominal is exactly $400000 (1.000000) except the last
# three, and every limit is a round number, which is what confirms the Q22
# reading of the format.
CAL_Q22 = 0x400000

#                 nominal    limit
CAL_ACCEPT = [(0x400000, 0x000400),      #  0  $C300
              (0x400000, 0x066666),      #  1  $C303
              (0x400000, 0x066666),      #  2  $C306
              (0x400000, 0x066666),      #  3  $C309
              (0x400000, 0x066666),      #  4  $C30C
              (0x400000, 0x066666),      #  5  $C30F
              (0x400000, 0x0CCCCC),      #  6  $C312
              (0x400000, 0x0CCCCC),      #  7  $C315
              (0x400000, 0x0CCCCC),      #  8  $C318
              (0x400000, 0x0CCCCC),      #  9  $C31B
              (0x0DE206, 0x062762),      # 10  $C31E
              (0x2AAAAA, 0x062762),      # 11  $C321
              (0x003126, 0x0147AE)]      # 12  $C324

CAL_CONSTS, CAL_TABLE, CAL_SPARE = 0xC300, 0xC327, 0xC357

# The six 8-byte records at $C327 are the calibration SETPOINTS, in the same
# 8-byte shape the firmware uses for a user memory at $C35C:
#
#   bytes 0-6   seven BCD digits, one per LOW nibble
#   bytes 0-6   exactly one HIGH nibble is 8; its position is the decimal point
#   byte  7     a decade / range code
#
# Confirmed against the ROM defaults at $EEF5, which decode under this rule to
# exactly 100.0000, 1.000000, 10.00000, 100.0000, 1.000000 and 10.00000 -- six
# round nominals, one per decade point.  A real instrument carries the same six
# values trimmed by a few hundred ppm, which is what a set of measured
# standards looks like.  The decade byte is never changed by calibration.
#
# What the decades mean in ohms is inference, not established: 0/1/1/1/2/2
# against those nominals is consistent with 100R, 1k, 10k, 100k, 1M, 10M, but
# the firmware routine that applies them was not traced.


def decode_setpoint(rec):
    """Decode one 8-byte setpoint record.  Returns (text, decade) or None."""
    if len(rec) < 8:
        return None
    hi = [b >> 4 for b in rec[:7]]
    lo = [b & 0x0F for b in rec[:7]]
    if hi.count(8) != 1 or any(h not in (0, 8) for h in hi) or any(d > 9 for d in lo):
        return None
    dp = hi.index(8)
    digits = "".join(str(d) for d in lo)
    return digits[:dp + 1] + "." + digits[dp + 1:], rec[7]


# ------------------------------------------------------------------ serial --
def _expect(s, want, what):
    line = s.readline().decode("ascii", "replace").strip()
    if line != want:
        raise IOError("%s: expected %r, got %r" % (what, want, line))
    return line


def _read_dump(s):
    """Consume records until END and return the 256 nibble values."""
    nib = [None] * 256
    while True:
        line = s.readline().decode("ascii", "replace").strip()
        if line == "END":
            break
        if not line:
            raise IOError("timed out waiting for data")
        if len(line) != 20:
            raise IOError("malformed record: %r" % line)
        base = int(line[0:2], 16)
        vals = [int(ch, 16) for ch in line[2:18]]
        if int(line[18:20], 16) != (sum(vals) & 0xFF):
            raise IOError("checksum error in record at $%02X" % base)
        nib[base:base + 16] = vals
    if any(v is None for v in nib):
        raise IOError("incomplete read")
    return bytes(nib)


def _records(nib):
    """Format a 256-nibble image as the 16 wire records."""
    out = []
    for base in range(0, 256, 16):
        vals = nib[base:base + 16]
        out.append("%02X%s%02X" % (base, "".join("%X" % (v & 0x0F) for v in vals),
                                   sum(v & 0x0F for v in vals) & 0xFF))
    return out


# Minimum sketch version.  Anything older is not merely out of date, it is
# wired for a different board, and the host refuses to talk to it.
#
#   3.0   THE PIN MAP CHANGED.  Everything before 3.0 expects /CS on PB2,
#         RECALL on PB3, /WE on PB4 and the data nibble on PD4-PD7.  On the
#         current wiring PD4 and PD5 are ARRAY RECALL and /WE, so a 2.x sketch
#         would drive the two most dangerous control lines with data patterns.
#         Nothing about that failure is quiet, but none of it is safe either.
#   2.2   documented the rotated pin map (/CS on chip pin 11, /WE on 9,
#         STORE on 7).  A rig built to that table has a data pin on STORE.
#   2.1   fixed the busIdle() ordering.  2.0 and earlier set DDRD before PORTD,
#         so STORE was driven LOW for a couple of instructions out of every
#         reset -- including the DTR reset caused by opening this serial port.
#         That is a valid store pulse, and it fired before any check here ran.
#   1.x   the old read-only sketch, retired in favour of one wiring and one
#         sketch.  If you have one flashed, reflash.
MIN_RW = 3.0


def _banner_version(banner):
    try:
        return float(banner.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return None


def _connect(port, baud=115200, need_rw=True):
    import serial                                     # pyserial
    s = serial.Serial(port, baud, timeout=5)
    time.sleep(2.0)                                   # board reset
    s.reset_input_buffer()
    s.write(b"I")
    banner = s.readline().decode("ascii", "replace").strip()
    if "X2212" not in banner:
        raise IOError("unexpected banner: %r" % banner)
    if "RW" not in banner:
        s.close()
        raise IOError("this is not the current sketch (%s); flash "
                      "rw_x2212_uno.ino from this folder" % banner)
    have = _banner_version(banner)
    if have is None or have < MIN_RW:
        s.close()
        raise IOError("sketch %r is older than %.1f and is UNSAFE - it expects "
                      "a different pin map; reflash from this folder before "
                      "going any further" % (banner, MIN_RW))
    print("   %s" % banner)
    return s


def write_device(port, nib, commit=False):
    """Load nib into the device RAM, verify, and optionally commit to EEPROM."""
    if len(nib) != 256:
        raise IOError("expected a 256-nibble image")
    s = _connect(port)

    # Read the part BEFORE arming anything.  Two jobs: it proves the whole
    # read path works on this wiring before a single write is attempted, and
    # it gives the dry run something to prove itself against -- "the array is
    # untouched" is only a real claim if we know what the array held first.
    s.reset_input_buffer()
    s.write(b"R")
    before = _read_dump(s)
    print("   read the device before touching it (%d locations)" % len(before))

    s.write(b"A WRITE-ENABLE\n")
    _expect(s, "ARMED", "arming")
    print("   armed")

    s.write(b"W")
    _expect(s, "SEND", "write handshake")
    for rec in _records(nib):
        s.write((rec + "\n").encode())
    line = s.readline().decode("ascii", "replace").strip()
    if line != "WROTE":
        raise IOError("write failed: %s" % line)
    back = _read_dump(s)
    if back != bytes(v & 0x0F for v in nib):
        bad = [i for i in range(256) if back[i] != (nib[i] & 0x0F)]
        raise IOError("RAM verify failed at %d location(s): %s"
                      % (len(bad), ", ".join("$%02X" % i for i in bad[:12])))
    print("   RAM written and verified (256 locations)")

    if not commit:
        s.write(b"D")
        _expect(s, "DISARMED", "disarm")
        s.write(b"R")                                 # recall: undo the RAM load
        after = _read_dump(s)
        s.close()
        if after != before:
            bad = [i for i in range(256) if after[i] != before[i]]
            raise IOError(
                "DRY RUN CHANGED THE ARRAY at %d location(s): %s -- the store "
                "line is doing something it should not.  Do not commit; check "
                "the STORE pull-up and the wiring before going further."
                % (len(bad), ", ".join("$%02X" % i for i in bad[:12])))
        print("   dry run: RAM loaded and verified, then recalled")
        print("   array recall returns the original contents - EEPROM untouched")
        return False

    s.write(b"S")
    line = s.readline().decode("ascii", "replace").strip()
    if line != "STORED":
        s.close()
        raise IOError("store refused: %s" % line)
    print("   store cycle issued")

    s.write(b"V")                                     # recall, then read back
    after = _read_dump(s)
    s.close()
    if after != bytes(v & 0x0F for v in nib):
        bad = [i for i in range(256) if after[i] != (nib[i] & 0x0F)]
        raise IOError("EEPROM verify failed at %d location(s): %s"
                      % (len(bad), ", ".join("$%02X" % i for i in bad[:12])))
    print("   array recall confirms the EEPROM holds the restored image")
    return True


def read_device(port, baud=115200, passes=3):
    s = _connect(port, baud)
    results = []
    for p in range(passes):
        s.reset_input_buffer()
        s.write(b"R")
        results.append(_read_dump(s))
        print("   pass %d complete" % (p + 1))
    s.close()

    if len(set(results)) == 1:
        return results[0]
    bad = [i for i in range(256) if len({r[i] for r in results}) > 1]
    raise IOError("passes disagree at %d location(s): %s"
                  % (len(bad), ", ".join("$%02X" % i for i in bad[:12])))


# ------------------------------------------------------------- nibble pack --
def unpack(nib, nbytes):
    """Two consecutive nibbles, low first, make one byte."""
    return bytes((nib[2 * i] & 0x0F) | ((nib[2 * i + 1] & 0x0F) << 4)
                 for i in range(nbytes))


def pack(block, total=256, fill=0x0F):
    """Inverse of unpack(): spread bytes into one nibble per element.

    Not exposed as a command. A device image built this way is NOT a faithful
    copy of a real part: the locations past the end of the block are filled
    with `fill`, whereas an actual device holds whatever its unwritten cells
    hold (A5/5A on the parts examined). Used to construct images for the
    tests, where the fill value is known and irrelevant."""
    nib = [fill] * total
    for i, b in enumerate(block):
        nib[2 * i] = b & 0x0F
        nib[2 * i + 1] = (b >> 4) & 0x0F
    return bytes(nib)


def q22(b, off):
    """The firmware's 3-byte number: signed 24-bit big-endian, 1.0 = $400000."""
    v = (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]
    if v & 0x800000:
        v -= 0x1000000
    return v


def sum16(b):
    """The firmware's checksum: successive 16-bit words, carry discarded."""
    s = 0
    for i in range(0, len(b) - 1, 2):
        s = (s + ((b[i] << 8) | b[i + 1])) & 0xFFFF
    return s


# ----------------------------------------------------------------- decode ---
def report_cal(cal):
    """Decode and print one calibration block. Returns True if it is sound."""
    ok = True
    print("Calibration block  RAM $%04X-$%04X" % (CAL_BASE, CAL_BASE + CAL_BYTES - 1))
    stored = (cal[CAL_SUM - CAL_BASE] << 8) | cal[CAL_SUM - CAL_BASE + 1]
    calc = sum16(cal[:CAL_LEN])
    print("   checksum  stored $%04X   computed $%04X   %s"
          % (stored, calc, "OK" if stored == calc else "MISMATCH"))
    if stored != calc:
        ok = False
        print("   !! The instrument would report CAL DATA BAD and overwrite")
        print("      these constants with the ROM defaults at next recall.")

    print()
    print("   Constants (see CAL_ACCEPT for the derivation):")
    print("   idx  addr    raw       value      nominal     |err|    limit   ")
    for i, (nom, lim) in enumerate(CAL_ACCEPT):
        off = 3 * i
        v = q22(cal, off)
        err = abs(v - nom)
        good = err < lim
        if not good:
            ok = False
        print("   %3d  $%04X  $%02X%02X%02X  %+10.6f  %+10.6f  %8.6f %8.6f  %s"
              % (i, CAL_CONSTS + off, cal[off], cal[off + 1], cal[off + 2],
                 v / CAL_Q22, nom / CAL_Q22, err / CAL_Q22, lim / CAL_Q22,
                 "ok" if good else "OUT"))
    if all(abs(q22(cal, 3 * i) - nom) < lim for i, (nom, lim) in enumerate(CAL_ACCEPT)):
        print("   all 13 constants inside the firmware's own acceptance windows")
    else:
        print("   !! at least one constant is outside the window SUB_E0A1 tests.")
        print("      The instrument would not print \" CAL DATA OK   \" for this")
        print("      data.  A correct checksum over wrong constants is exactly")
        print("      what a bad read looks like -- re-read before trusting it.")

    print()
    print("   Setpoints, 6 records of 8 bytes ($C327-$C356):")
    for r in range(6):
        off = CAL_TABLE - CAL_BASE + 8 * r
        rec = cal[off:off + 8]
        d = decode_setpoint(rec)
        if d is None:
            print("   %d  $%04X  %s   (does not fit the BCD setpoint format)"
                  % (r, CAL_TABLE + 8 * r, " ".join("%02X" % b for b in rec)))
        else:
            print("   %d  $%04X  %s   %12s   decade %d"
                  % (r, CAL_TABLE + 8 * r, " ".join("%02X" % b for b in rec),
                     d[0], d[1]))
    print()
    print("   $%04X  %02X   (in the checksum; not written by the ROM defaults)"
          % (CAL_SPARE, cal[CAL_SPARE - CAL_BASE]))
    print()
    print("   Raw (88 bytes, $C300-$C357):")
    for r in range(0, CAL_LEN, 16):
        print("   %04X  %s" % (CAL_BASE + r,
                               " ".join("%02X" % b for b in cal[r:r + 16])))
    return ok


def report_user(usr):
    """Decode and print one user block. Returns True if it is sound."""
    ok = True
    print("User block         RAM $%04X-$%04X" % (USR_BASE, USR_BASE + USR_BYTES - 1))
    stored = (usr[USR_SUM - USR_BASE] << 8) | usr[USR_SUM - USR_BASE + 1]
    calc = sum16(usr[:USR_LEN])
    print("   checksum  stored $%04X   computed $%04X   %s"
          % (stored, calc, "OK" if stored == calc else "MISMATCH"))
    if stored != calc:
        ok = False

    # $C35A and $C35B are the TENS and UNITS digits of the IEEE-488 address,
    # not a packed configuration word.  SUB_F277 forms the address by loading
    # $C35B into A, $C35A into B, adding 10 to A B times, masking to 5 bits and
    # writing the result to the GPIB controller at $1004.  The address-entry
    # routine at L_F7F2 rolls the old units digit into $C35A (clamping it to
    # 0-2, since 2*10+9 = 29 is the largest that survives the mask) and puts
    # the new digit in $C35B.  The firmware's own default is $C35A=0, $C35B=9,
    # written by L_E1B4/L_E1D9 -- address 9.
    addr = (usr[0] * 10 + usr[1]) & 0x1F
    print("   address digits  tens $%02X  units $%02X  ->  IEEE-488 address %d"
          % (usr[0], usr[1], addr))
    if usr[0] > 2 or usr[1] > 9:
        print("   !! digits out of range - the instrument would mask this to %d" % addr)
    for m in range(10):
        off = 2 + m * 8
        if off + 8 <= USR_LEN:
            print("   memory %-2d  %s" % (m, " ".join("%02X" % b for b in usr[off:off + 8])))
    return ok


def check(*nibs, **kw):
    """Decode and verify one or more device images.

    Each image is identified by its OWN checksum, not by the position it was
    passed in.  A calibration image is therefore never decoded against the user
    block's layout, or the reverse, however the files are given or picked.

    Returns (ok, {"cal": bytes or None, "user": bytes or None}).
    """
    names = kw.get("names") or [None] * len(nibs)
    ok = True
    out = {"cal": None, "user": None}

    for nib, name in zip(nibs, names):
        kind = identify_block(nib)
        print()
        if name:
            print("%s  identifies as %s" % (name, DEVNAME.get(kind, "neither block type")))
        if kind == "cal":
            out["cal"] = unpack(nib, CAL_BYTES)
            ok &= report_cal(out["cal"])
        elif kind == "user":
            out["user"] = unpack(nib, USR_BYTES)
            ok &= report_user(out["user"])
        elif kind == "both":
            ok = False
            print("   This image satisfies BOTH block layouts, so its type cannot")
            print("   be established from the file alone. Nothing is decoded.")
        else:
            ok = False
            print("   This image checks out as neither block type, so nothing is")
            print("   decoded. Its checksum against each layout:")
            for who, nbytes, dlen in (("calibration", CAL_BYTES, CAL_LEN),
                                      ("user", USR_BYTES, USR_LEN)):
                blk = unpack(nib, nbytes)
                st = (blk[dlen] << 8) | blk[dlen + 1]
                print("      as a %-11s block  stored $%04X  computed $%04X"
                      % (who, st, sum16(blk[:dlen])))

    return bool(ok), out


# ------------------------------------------------------------------- menu ---
BAR = "=" * 66


def ask(prompt, default=None):
    d = " [%s]" % default if default is not None else ""
    while True:
        v = input("%s%s: " % (prompt, d)).strip()
        if v:
            return v
        if default is not None:
            return default


def ask_yes(prompt, default=False):
    d = "Y/n" if default else "y/N"
    while True:
        v = input("%s (%s): " % (prompt, d)).strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def ask_choice(prompt, options):
    """options: list of (key, label). Returns the key."""
    print()
    for k, label in options:
        print("   %-3s %s" % (k, label))
    keys = {k.lower() for k, _ in options}
    while True:
        v = input("%s: " % prompt).strip().lower()
        if v in keys:
            return v


def pick_port(current=None):
    ports = []
    try:
        from serial.tools import list_ports
        ports = [(p.device, p.description or "") for p in list_ports.comports()]
    except Exception:
        pass
    if ports:
        print()
        print("Serial ports found:")
        for i, (dev, desc) in enumerate(ports, 1):
            print("   %d  %s   %s" % (i, dev, desc))
        v = ask("Choose a number, or type a port", "1" if current is None else current)
        if v.isdigit() and 1 <= int(v) <= len(ports):
            return ports[int(v) - 1][0]
        return v
    # Nothing enumerated. Offer a plausible default for the platform rather
    # than a Linux device node on a Windows box.
    if current is None:
        current = "COM3" if sys.platform.startswith("win") else "/dev/ttyACM0"
    return ask("Serial port", current)


def pick_file(pattern, prompt):
    """List the files matching pattern and take a number, a path, or Enter."""
    import glob
    found = sorted(glob.glob(pattern))
    if not found:
        while True:
            v = ask(prompt)
            if os.path.exists(v):
                return v
            print("   no such file: %s" % v)

    print()
    for i, f in enumerate(found, 1):
        print("   %d  %s  (%d bytes)" % (i, f, os.path.getsize(f)))

    default = found[0]
    while True:
        v = ask("%s - number, path, or Enter" % prompt, default)
        if v.isdigit():
            n = int(v)
            if 1 <= n <= len(found):
                return found[n - 1]
            print("   there is no %d in the list" % n)
            continue
        if os.path.exists(v):
            return v
        print("   no such file: %s" % v)


def identify_block(nib):
    """Which block type does this image check out as?  'cal', 'user' or None.

    An image can in principle satisfy both layouts -- the odds are about one in
    65536 per layout, so it is not impossible, and silently preferring 'cal'
    would defeat the wrong-chip refusal in the restore path.  Ambiguity is
    reported rather than guessed at."""
    hits = []
    for dev, nbytes, dlen in (("cal", CAL_BYTES, CAL_LEN), ("user", USR_BYTES, USR_LEN)):
        blk = unpack(nib, nbytes)
        stored = (blk[dlen] << 8) | blk[dlen + 1]
        if stored == sum16(blk[:dlen]):
            hits.append(dev)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return "both"
    return None


DEVNAME = {"cal": "IC522 - calibration block", "user": "IC523 - user block"}


def menu_backup(state):
    print()
    print(BAR)
    print("BACK UP A DEVICE")
    print(BAR)
    print("Reading cannot change the array. An array recall only ever copies")
    print("EEPROM into RAM, and R never arms the sketch - the write and store")
    print("commands are refused until an explicit token is sent, which backing")
    print("up never does.")
    dev = ask_choice("Which chip is in the socket?",
                     [("1", DEVNAME["cal"]), ("2", DEVNAME["user"])])
    dev = "cal" if dev == "1" else "user"
    default = "ic522.bin" if dev == "cal" else "ic523.bin"
    out = ask("Save to", default)
    if os.path.exists(out) and not ask_yes("%s exists. Overwrite?" % out):
        print("   cancelled")
        return
    passes = int(ask("Passes (all must agree)", "3"))
    print()
    print("Reading %s" % DEVNAME[dev])
    try:
        nib = read_device(state["port"], passes=passes)
    except IOError as e:
        print("   READ FAILED: %s" % e)
        return
    except Exception as e:
        print("   could not open %s: %s" % (state["port"], e))
        return
    open(out, "wb").write(nib)
    used = 180 if dev == "cal" else 168
    print("   wrote %s (256 nibbles, %d in use)" % (out, used))
    kind = identify_block(nib)
    if kind == dev:
        print("   checksum: valid %s block" % dev)
    elif kind is None:
        print("   WARNING: this image does not check out as either block type.")
        print("   Either the read is wrong, or the stored data is already bad.")
    else:
        print("   WARNING: this checks out as the %s block, not %s." % (kind, dev))
        print("   You may have the other chip in the socket.")


def menu_check(state):
    print()
    print(BAR)
    print("CHECK A SAVED IMAGE")
    print(BAR)
    print("The image is identified by its own checksum, so it does not have to")
    print("be said which device it came from.")
    f = pick_file("*.bin", "Image to check")
    nib = open(f, "rb").read()
    if len(nib) != 256:
        print("   %s is %d bytes, not 256" % (f, len(nib)))
        return
    ok, _ = check(nib, names=[f])
    print()
    print("   %s" % ("the image checks out" if ok else "THIS IMAGE IS BAD"))


def menu_restore(state):
    print()
    print(BAR)
    print("RESTORE A DEVICE  --  this can overwrite the calibration")
    print(BAR)

    src = pick_file("*.bin", "Image to restore")
    if src is None:
        return
    nib = open(src, "rb").read()
    if len(nib) != 256:
        print("   expected a 256-byte nibble image, got %d" % len(nib))
        return

    kind = identify_block(nib)
    if kind == "both":
        print()
        print("   This image satisfies BOTH block layouts, so its type cannot be")
        print("   established from the file alone and the wrong-chip check below")
        print("   would be worthless. Refusing.")
        return
    if kind is None:
        print()
        print("   This image does not check out as either block type.")
        print("   Restoring it would leave the instrument with data its own")
        print("   checksum rejects. Refusing.")
        return
    print()
    print("   %s checks out as: %s" % (src, DEVNAME[kind]))
    print("   Target: %s. The device in the socket is read first and refused if" % DEVNAME[kind])
    print("   it turns out to be the other one.")

    print()
    print("   Step 1 of 2: dry run. This loads the device RAM and verifies it,")
    print("   then recalls from the EEPROM array so nothing is left changed.")
    if not ask_yes("   Run the dry run now?", True):
        return
    try:
        write_device(state["port"], nib, commit=False)
    except IOError as e:
        print("   DRY RUN FAILED: %s" % e)
        print("   Nothing was committed. Fix this before going further.")
        return
    except Exception as e:
        print("   could not open %s: %s" % (state["port"], e))
        return

    print()
    print("   Dry run passed. The EEPROM array has not been touched.")
    print()
    print("   Step 2 of 2: commit. This issues one store cycle and permanently")
    print("   replaces the contents of %s." % DEVNAME[kind])
    print("   Source : %s" % src)
    print("   Target : %s" % DEVNAME[kind])
    print()
    if input("   Type COMMIT in capitals to proceed, anything else to stop: ").strip() != "COMMIT":
        print("   Stopped. Nothing was written to the array.")
        return
    try:
        write_device(state["port"], nib, commit=True)
    except IOError as e:
        print("   COMMIT FAILED: %s" % e)
        return
    print()
    print("   Restored and verified against the EEPROM array.")
    print("   Refit the chip, power-cycle the instrument, and confirm it does")
    print("   not report CAL DATA BAD.")


def menu():
    state = {"port": None}
    print()
    print(BAR)
    print("Valhalla Scientific 2724A  --  NOVRAM tool  (IC522 / IC523, X2212)")
    print(BAR)
    state["port"] = pick_port()
    while True:
        print()
        print(BAR)
        print("Port: %s" % state["port"])
        c = ask_choice("Choose", [
            ("1", "Back up a device        (read a chip to a file)"),
            ("2", "Check a saved image     (decode and verify checksums)"),
            ("3", "Restore a device        (write a file back to a chip)"),
            ("4", "Change serial port"),
            ("q", "Quit"),
        ])
        if c == "1":
            menu_backup(state)
        elif c == "2":
            menu_check(state)
        elif c == "3":
            menu_restore(state)
        elif c == "4":
            state["port"] = pick_port(state["port"])
        elif c == "q":
            print()
            return 0


# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("read", help="read one device through the Arduino")
    r.add_argument("port")
    r.add_argument("--dev", choices=["cal", "user"], required=True,
                   help="cal = IC522 (bits 0-3), user = IC523 (bits 4-7)")
    r.add_argument("-o", "--out", required=True)
    r.add_argument("--passes", type=int, default=3)

    c = sub.add_parser("check",
                       help="decode and verify one or more device images")
    c.add_argument("images", nargs="+",
                   help="device images; each is identified by its own checksum, "
                        "so the order does not matter")

    w = sub.add_parser("write", help="restore a saved image to a device")
    w.add_argument("port")
    w.add_argument("--dev", choices=["cal", "user"], required=True,
                   help="which device is in the socket")
    w.add_argument("--image", required=True,
                   help="256-byte device image to restore")
    w.add_argument("--commit", action="store_true",
                   help="actually commit to the EEPROM array (default is a dry run)")
    w.add_argument("--force", action="store_true",
                   help="skip the check that the image matches --dev")

    if len(sys.argv) == 1:                 # no arguments: interactive menu
        try:
            return menu()
        except (KeyboardInterrupt, EOFError):
            print("\ncancelled")
            return 1

    a = ap.parse_args()

    if a.cmd == "read":
        print("Reading %s (%s)" % (a.dev, "IC522" if a.dev == "cal" else "IC523"))
        try:
            nib = read_device(a.port, passes=a.passes)
        except IOError as e:
            sys.exit("read failed: %s" % e)
        open(a.out, "wb").write(nib)
        used = 180 if a.dev == "cal" else 168
        print("   wrote %s (256 nibbles; %d in use)" % (a.out, used))

    elif a.cmd == "check":
        nibs, names = [], []
        for f in a.images:
            d = open(f, "rb").read()
            if len(d) != 256:
                sys.exit("%s is %d bytes; expected a 256-byte device image"
                         % (f, len(d)))
            nibs.append(d)
            names.append(f)
        ok, _ = check(*nibs, names=names)
        return 0 if ok else 1

    elif a.cmd == "write":
        nib = open(a.image, "rb").read()
        if len(nib) != 256:
            sys.exit("expected a 256-byte nibble image, got %d" % len(nib))
        nbytes = CAL_BYTES if a.dev == "cal" else USR_BYTES
        dlen = CAL_LEN if a.dev == "cal" else USR_LEN
        blk = unpack(nib, nbytes)
        stored = (blk[dlen] << 8) | blk[dlen + 1]
        calc = sum16(blk[:dlen])
        who = "IC522 (calibration)" if a.dev == "cal" else "IC523 (user)"
        print("Restoring to %s" % who)
        print("   source %s: checksum stored $%04X computed $%04X  %s"
              % (a.image, stored, calc, "OK" if stored == calc else "MISMATCH"))
        if stored != calc and not a.force:
            sys.exit("refusing: this image does not check out as a %s block.\n"
                     "Writing the wrong block into this device would destroy it.\n"
                     "Use --force only if you are certain." % a.dev)
        if not a.commit:
            print("   DRY RUN - the EEPROM array will not be touched")
        try:
            committed = write_device(a.port, nib, commit=a.commit)
        except IOError as e:
            sys.exit("write failed: %s" % e)
        if committed:
            print("\nRestored. Power-cycle the instrument and confirm it does not")
            print("report CAL DATA BAD.")
        else:
            print("\nDry run complete. Re-run with --commit to write it for real.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
