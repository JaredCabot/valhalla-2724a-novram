#!/usr/bin/env python3
"""End-to-end tests for the NOVRAM tool against a simulated X2212.

The emulator models the part properly: a volatile static RAM and a separate
non-volatile EEPROM array, with recall copying array -> RAM and store copying
RAM -> array.  That is what makes the dry-run and store tests meaningful.
"""
import os
import pty
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import novram as N

def pack(block, total=256, fill=0x0F):
    """Inverse of N.unpack(): spread bytes into one nibble per element.

    A test fixture, not a faithful device image -- the locations past the end
    of the block get `fill`, where a real part holds whatever its unwritten
    cells hold (A5/5A on the parts examined)."""
    nib = [fill] * total
    for i, b in enumerate(block):
        nib[2 * i] = b & 0x0F
        nib[2 * i + 1] = (b >> 4) & 0x0F
    return bytes(nib)


BANNER_RW = "X2212-NOVRAM v1.0.0"
BANNER_OLD = "X2212-NOVRAM-RW UNO 3.0"          # a pre-1.0.0 sketch
TOKEN = "WRITE-ENABLE"


class Fake:
    """A simulated X2212 speaking the sketch's serial protocol."""

    def __init__(self, eeprom, banner=BANNER_RW, corrupt_record=None,
                 flaky=None, bad_store=None, glitch_store=False):
        self.eeprom = bytearray(eeprom)
        self.ram = bytearray([0x0F] * 256)      # indeterminate until recall
        self.armed = False
        self.verified = False
        self.banner = banner
        self.corrupt_record = corrupt_record
        self.flaky = flaky
        self.bad_store = bad_store              # a cell the array will not take
        # A spurious store: the array quietly takes whatever RAM holds at a
        # moment nobody asked for.  "write" fires it just after the RAM has
        # been loaded with the new image -- the moment that actually matters,
        # which is the residual hazard once the sketch is correct -- a marginal
        # STORE line, a missing pull-up, a noisy lead.  That is what the host's
        # before/after comparison is there to catch.
        #
        # There is deliberately no "connect" mode.  The busIdle() ordering bug
        # in sketches up to RW 2.0 fires its store during the reset that opening
        # the port causes, i.e. before the host has read anything, so there is
        # no "before" to compare against and NO host-side check can catch it.
        # That is precisely why the version guard in _connect exists, and why it
        # is tested separately above rather than being left to this mechanism.
        self.glitch_store = glitch_store
        self.npass = 0
        self.buf = b""

    def run(self, fd):
        def out(line):
            os.write(fd, (line + "\r\n").encode())

        def getch():
            while not self.buf:
                try:
                    ch = os.read(fd, 1)
                except OSError:
                    return None
                if not ch:
                    return None
                self.buf += ch
            c, self.buf = self.buf[:1], self.buf[1:]
            return c.decode(errors="replace")

        def readline():
            t0 = time.time()
            while b"\n" not in self.buf:
                if time.time() - t0 > 5:
                    return None
                try:
                    ch = os.read(fd, 1)
                except OSError:
                    return None
                if not ch:
                    return None
                self.buf += ch
            line, _, self.buf = self.buf.partition(b"\n")
            return line.decode(errors="replace").strip()

        def dump():
            self.npass += 1
            for base in range(0, 256, 16):
                vals = list(self.ram[base:base + 16])
                if self.flaky is not None and self.npass == 1 and \
                   base <= self.flaky < base + 16:
                    vals[self.flaky - base] ^= 1
                s = sum(vals) & 0xFF
                if self.corrupt_record == base:
                    s ^= 0xFF
                out("%02X%s%02X" % (base, "".join("%X" % v for v in vals), s))
            out("END")

        while True:
            c = getch()
            if c is None:
                return
            if c in "\r\n":
                continue

            if c == "I":
                out(self.banner)

            elif c == "R":
                self.ram = bytearray(self.eeprom)          # array recall
                dump()

            elif c == "A":
                line = readline()
                if line is not None and line.strip() == TOKEN:
                    self.armed = True
                    out("ARMED")
                else:
                    self.armed = False
                    out("LOCKED")

            elif c == "D":
                self.armed = False
                self.verified = False
                out("DISARMED")

            elif c == "W":
                self.verified = False
                if not self.armed:
                    out("LOCKED")
                    continue
                out("SEND")
                got = bytearray(256)
                ok = True
                for _ in range(16):     # noqa: B007 - _ is the record index
                    line = readline()
                    if line is None or len(line) != 20:
                        out("ERR format")
                        ok = False
                        break
                    base = int(line[0:2], 16)
                    if base != (_ * 16):            # records must be in order
                        out("ERR order")
                        ok = False
                        break
                    vals = [int(x, 16) for x in line[2:18]]
                    if int(line[18:20], 16) != (sum(vals) & 0xFF):
                        out("ERR checksum")
                        ok = False
                        break
                    got[base:base + 16] = vals
                if not ok:
                    continue
                self.ram = got                             # RAM write
                if self.glitch_store == "write":
                    self.eeprom = bytearray(self.ram)      # spurious store
                self.verified = True
                out("WROTE")
                dump()

            elif c == "S":
                if not self.armed:
                    out("LOCKED")
                elif not self.verified:
                    out("ERR no verified write")
                else:
                    self.eeprom = bytearray(self.ram)      # store
                    if self.bad_store is not None:
                        self.eeprom[self.bad_store] ^= 1   # one cell refuses
                    self.armed = False
                    self.verified = False
                    out("STORED")


def run(fake, fn):
    master, slave = pty.openpty()
    port = os.ttyname(slave)
    threading.Thread(target=fake.run, args=(master,), daemon=True).start()
    old = N.time.sleep
    N.time.sleep = lambda s: None                  # skip the board-reset delay
    try:
        return fn(port), None
    except IOError as e:
        return None, str(e)
    finally:
        N.time.sleep = old
        os.close(slave)




# ---------------------------------------------------------------- menu ------
def drive(fake, fn, answers):
    """Run a menu function with scripted keyboard input against a fake chip."""
    import builtins
    master, slave = pty.openpty()
    port = os.ttyname(slave)
    threading.Thread(target=fake.run, args=(master,), daemon=True).start()
    it = iter(answers)
    old_input, old_sleep = builtins.input, N.time.sleep
    builtins.input = lambda prompt="": next(it)
    N.time.sleep = lambda s: None
    try:
        fn(port)
    finally:
        builtins.input = old_input
        N.time.sleep = old_sleep
        os.close(slave)


def menu_tests(check):
    import tempfile
    cal = bytearray(range(N.CAL_LEN)) + bytearray(2)
    s = N.sum16(cal[:N.CAL_LEN])
    cal[N.CAL_LEN], cal[N.CAL_LEN + 1] = s >> 8, s & 0xFF
    cal_nib = pack(bytes(cal))

    usr = bytearray((i * 3 + 1) & 0xFF for i in range(N.USR_LEN)) + bytearray(2)
    s = N.sum16(usr[:N.USR_LEN])
    usr[N.USR_LEN], usr[N.USR_LEN + 1] = s >> 8, s & 0xFF
    usr_nib = pack(bytes(usr))

    junk = bytes((i * 11) & 0x0F for i in range(256))

    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(d)
    open("cal.bin", "wb").write(cal_nib)
    open("user.bin", "wb").write(usr_nib)
    open("junk.bin", "wb").write(junk)
    onchip = pack(bytes(range(90)))
    try:
        print("9. menu: an image that checks out as neither block is refused")
        f = Fake(onchip)
        drive(f, N.menu_restore, ["junk.bin"])
        check("array untouched", bytes(f.eeprom) == onchip)

        print("12. menu: dry run runs, but nothing commits without COMMIT")
        f = Fake(onchip)
        drive(f, N.menu_restore, ["cal.bin", "y", "yes please"])
        check("array untouched", bytes(f.eeprom) == onchip)
        check("RAM left as the array had it", bytes(f.ram) == onchip)

        print("13. menu: the full path with COMMIT does write the array")
        f = Fake(onchip)
        drive(f, N.menu_restore, ["cal.bin", "y", "COMMIT"])
        check("array holds the restored image", bytes(f.eeprom) == cal_nib)
        check("auto-disarmed", f.armed is False)

        print("14. menu: backup writes the file and identifies the block")
        f = Fake(cal_nib)
        drive(f, N.menu_backup, ["1", "out.bin", "3"])
        check("file written", os.path.exists("out.bin"))
        check("content matches the chip",
              os.path.exists("out.bin") and open("out.bin", "rb").read() == cal_nib)
    finally:
        os.chdir(cwd)


def main():
    original = pack(bytes(range(90)))            # what the chip holds now
    restore = pack(bytes((i * 7 + 3) & 0xFF for i in range(90)))
    fails = 0

    def check(label, cond, detail=""):
        nonlocal fails
        print("   %-42s %s %s" % (label, "PASS" if cond else "FAIL",
                                  ("- " + detail) if detail and not cond else ""))
        if not cond:
            fails += 1

    print("1. read back what the array holds")
    d = Fake(original)
    got, err = run(d, lambda p: N.read_device(p, passes=3))
    check("image matches", got == original, err or "")

    print("2. dry run must not touch the EEPROM array")
    d = Fake(original)
    res, err = run(d, lambda p: N.write_device(p, restore, commit=False))
    check("reports not committed", res is False, err or "")
    check("EEPROM unchanged", bytes(d.eeprom) == original)
    check("RAM restored by array recall", bytes(d.ram) == original)
    check("left disarmed", d.armed is False)

    print("3. commit writes the array and proves it by recall")
    d = Fake(original)
    res, err = run(d, lambda p: N.write_device(p, restore, commit=True))
    check("reports committed", res is True, err or "")
    check("EEPROM holds the restored image", bytes(d.eeprom) == restore)
    check("auto-disarmed after store", d.armed is False)

    print("4. a store the array does not take must be caught")
    d = Fake(original, bad_store=0x42)
    res, err = run(d, lambda p: N.write_device(p, restore, commit=True))
    check("write reports failure", res is None and err is not None)
    check("error names the EEPROM stage",
          err is not None and "EEPROM verify failed" in err, err or "")

    print("5. the retired read-only sketch must be refused, for reads too")
    d = Fake(original, banner=BANNER_OLD)
    res, err = run(d, lambda p: N.write_device(p, restore, commit=True))
    check("refused for writing", res is None)
    check("error names the sketch to flash",
          err is not None and "rw_x2212_uno.ino" in err, err or "")
    d = Fake(original, banner=BANNER_OLD)
    got, err = run(d, lambda p: N.read_device(p, passes=3))
    check("refused for reading as well", got is None, err or "")

    print("6. corrupted record on the way back must be rejected")
    d = Fake(original, corrupt_record=0x20)
    got, err = run(d, lambda p: N.read_device(p, passes=3))
    check("rejected", got is None and "checksum" in (err or ""), err or "")

    print("15. a sketch older than the safe minimum must be refused")
    d = Fake(original, banner="X2212-NOVRAM v0.9.0")
    res, err = run(d, lambda p: N.write_device(p, restore, commit=True))
    check("refused", res is None)
    check("error says it is too old",
          err is not None and "older than the required" in err, err or "")
    check("array untouched", bytes(d.eeprom) == original)

    d = Fake(original, banner="X2212-NOVRAM v0.9.0")
    got, err = run(d, lambda p: N.read_device(p, passes=3))
    check("old sketch refused for reads too",
          got is None and "older than the required" in (err or ""), err or "")

    print("16. a spurious store during the dry run must be caught")
    d = Fake(original, glitch_store="write")
    res, err = run(d, lambda p: N.write_device(p, restore, commit=False))
    check("dry run reports failure", res is None and err is not None)
    check("error names the array",
          err is not None and "DRY RUN CHANGED THE ARRAY" in err, err or "")

    print("17. calibration constants are checked against the firmware's windows")
    cal = bytearray(90)
    for i, (nom, _lim) in enumerate(N.CAL_ACCEPT):          # ROM defaults
        cal[3 * i], cal[3 * i + 1], cal[3 * i + 2] = \
            (nom >> 16) & 0xFF, (nom >> 8) & 0xFF, nom & 0xFF
    def _seal(b, dlen):
        v = N.sum16(bytes(b[:dlen]))
        b[dlen], b[dlen + 1] = v >> 8, v & 0xFF
        return bytes(b)
    usr = bytearray(84)
    usr[0], usr[1] = 1, 4                                   # address 14
    good_cal = _seal(cal, N.CAL_LEN)
    good_usr = _seal(usr, N.USR_LEN)
    ok, d = N.check(pack(good_cal), pack(good_usr))
    check("ROM-default constants are accepted", ok is True)
    check("q22 reads 1.000000", abs(N.q22(d["cal"], 0) / N.CAL_Q22 - 1.0) < 1e-6)
    check("two-digit bus address decodes to 14",
          (d["user"][0] * 10 + d["user"][1]) & 0x1F == 14)

    print("17b. order does not matter, and a user image is never decoded as cal")
    ok2, d2 = N.check(pack(good_usr), pack(good_cal))     # reversed
    check("same verdict with the arguments swapped", ok2 is True)
    check("the cal image still landed in the cal slot", d2["cal"] == d["cal"])
    check("the user image still landed in the user slot", d2["user"] == d["user"])
    ok3, d3 = N.check(pack(good_usr))                        # user image alone
    check("a user image alone checks out", ok3 is True)
    check("and no calibration decode was attempted", d3["cal"] is None)

    bad = bytearray(good_cal)
    v = N.CAL_ACCEPT[1][0] + N.CAL_ACCEPT[1][1] + 1          # just outside
    bad[3], bad[4], bad[5] = (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
    ok, _ = N.check(pack(_seal(bad, N.CAL_LEN)), pack(good_usr))
    check("a constant outside its window is flagged", ok is False)

    print("7. one unstable nibble in a single pass must be caught")
    d = Fake(original, flaky=0x35)
    got, err = run(d, lambda p: N.read_device(p, passes=3))
    check("rejected", got is None and "disagree" in (err or ""), err or "")

    print("8. block-type guard (checked before anything is sent)")
    cal = bytearray(range(N.CAL_LEN)) + bytearray(2)
    s = N.sum16(cal[:N.CAL_LEN])
    cal[N.CAL_LEN], cal[N.CAL_LEN + 1] = s >> 8, s & 0xFF
    good = N.unpack(pack(bytes(cal)), N.CAL_BYTES)
    st = (good[N.CAL_LEN] << 8) | good[N.CAL_LEN + 1]
    check("a real cal image checks out", st == N.sum16(good[:N.CAL_LEN]))
    other = N.unpack(restore, N.CAL_BYTES)
    st = (other[N.CAL_LEN] << 8) | other[N.CAL_LEN + 1]
    check("an unrelated image does not", st != N.sum16(other[:N.CAL_LEN]))

    menu_tests(check)

    print()
    print("all tests passed" if fails == 0 else "%d check(s) FAILED" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
