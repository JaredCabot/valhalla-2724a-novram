# 2724A calibration NOVRAM — IC522 / IC523

Backup and restore of the calibration constants held in the two NOVRAMs of a
**Valhalla Scientific 2724A** Programmable Precision Resistance Standard, using
an Arduino Uno.

IC522 holds 88 bytes of calibration data unique to one instrument. If its
checksum fails, the firmware loads a default block from ROM without halting,
and the instrument reads incorrectly while otherwise operating normally. IC523
holds the user block: the IEEE-488 address and ten user memories.

The pin map, data layout and number formats documented here are derived from
drawing 2724-070 sheet 5, the Xicor X2212 and NCR 52212 datasheets, and a
disassembly of the 6809 firmware.

**Validation status.** Both paths are verified against hardware. Both devices of
one instrument have been read, each twice, with matching checksums and
bit-identical results. The restore path has been exercised in full on a real
device: a dry run confirmed the array untouched, a commit issued one store
cycle, and an independent re-read returned contents byte-for-byte identical to
the backup taken beforehand, with a valid checksum and all thirteen constants
inside the firmware's acceptance windows. `test_protocol.py` additionally
covers the refusal paths, which cannot be exercised on hardware without
risking the data.

**Calibration images are not distributed with this repository.** A device image
holds the contents of one device of one specific instrument: 256 bytes, one per
nibble, of which 180 are in use. Loading another instrument's calibration image
produces a machine that satisfies its own checksum and reads incorrectly.

Licensed MIT — see `LICENSE`.

---

## The devices

Drawing 2724-070 sheet 5 marks both devices **X2212-30**: a 256 x 4 nonvolatile
static RAM in an 18-pin DIP on 300 mil centres. The parts fitted are marked
**NCR 52212**, NCR's second source for the same device.

The firmware confirms the size independently. The store routine at `$E0FA`
addresses 180 consecutive locations from `$A000`, which requires a 256-word
part rather than the 64-word X2210.

The two datasheets differ, and the NCR figures govern:

| | Xicor X2212 | NCR 52212 |
|---|---|---|
| read cycle `tRC` | 300 ns min | 300 ns min |
| write pulse `tWP` | 150 ns min | 150 ns min |
| address setup `tAS` | 50 ns min | 50 ns min |
| store pulse `tSTP` | 100 ns min | 100 ns min |
| store time `tSTC` | 10 ms max | 10 ms max |
| recall pulse `tRCP` | 450 ns min | 200 ns min |
| **recall cycle `tRCC`** | **1200 ns min** | **30 µs typ, 70 µs max** |
| store cycles | 10 000 (100 000 on the `/10`) | 10 000 min |
| data changes per bit | 1 000 (10 000 on the `/10`) | — |

`tRCC` is the significant difference. The NCR part can take seventy
microseconds to complete an array recall. A recall sampled before that time
returns a partially recalled RAM, and since every pass recalls identically,
repeated passes agree with one another and yield a stable, correctly
checksummed, incorrect image. The sketch allows 150 µs on each side of the
recall pulse.

The `-30` in "X2212-30" is a speed grade, not an endurance grade, and the
fitted parts are not the `/10` variant. The applicable endurance figures are
**10 000 store cycles and 1 000 data changes per bit**.

## Write protection

`STORE` (pin 9) transfers the volatile RAM into the EEPROM array. `/WE`
(pin 11) enables writes to the RAM. Both are active low.

Every Arduino pin is a high-impedance input at power-up, during reset, and
throughout a sketch upload. With nothing holding these two lines they float
through all three windows, and a floating `STORE` can commit the contents of
the volatile RAM into the EEPROM array. Xicor application note AN-101 addresses
spurious `STORE` pulses at power-up and brown-out as a known field failure.

**10k from `/WE` to +5V and 10k from `STORE` to +5V are required, fitted before
the chip is inserted.**

A third resistor gives a second, independent inhibit. Both datasheets state
that holding `STORE` high **or** `RECALL` low prevents an unintentional store;
the NCR text reads *"During power up or power down, precaution must be taken to
prevent an unintentional Store cycle. Holding STORE high or RECALL low will
inhibit the initiation of a Store cycle."* **10k from `ARRAY RECALL` (pin 10) to
ground** implements the second method. While the Arduino pins are
high-impedance, `RECALL` is held low and the store is inhibited inside the chip
irrespective of the state of the `STORE` pin. A low `RECALL` copies EEPROM into
RAM only, and holds the I/O pins in high-Z; with the sketch running the pin is
driven high against 0.5 mA. On `STORE` a pull-down would assert the condition
being guarded against.

## Supply decoupling

A **0.1 µF ceramic capacitor between pins 18 and 8, mounted at the socket**, is
required.

The device sits on flying leads rather than the ground plane it was designed
into, so its supply presents considerably more inductance than the instrument's
board does. Supply current is not constant: both datasheets rate `Icc` at
50-60 mA maximum, and the peak falls during the store cycle, when the EEPROM
array is written. Both parts also inhibit all operations when `Vcc` falls to
about 3 V, so a transient large enough to reach that threshold during a store
interrupts it.

Lead length is the whole point of the capacitor, so it belongs across the socket
pins themselves rather than at the Arduino's supply header.

## Pin map

Verified against drawing 2724-070 sheet 5, the Xicor X2212 datasheet and the
NCR 52212 datasheet, which agree.

| Chip pin | Signal | Uno pin | AVR port | Instrument net |
|---|---|---|---|---|
| 1 | A7 | A0 | PC0 | A7 |
| 2 | A4 | A1 | PC1 | A4 |
| 3 | A3 | A2 | PC2 | A3 |
| 4 | A2 | A3 | PC3 | A2 |
| 5 | A1 | A4 | PC4 | A1 |
| 6 | A0 | A5 | PC5 | A0 |
| **7** | **/CS** | D3 | PD3 | `NOV`, the 74LS138 decode |
| 8 | GND | GND | | 0V |
| **9** | **STORE** | **D2 + 10k to +5V** | PD2 | `WE`, strobe at `$2000` |
| **10** | **ARRAY RECALL** | **D4 + 10k to GND** | PD4 | `RE`, strobe at `$4000` |
| **11** | **/WE** | **D5 + 10k to +5V** | PD5 | `R`, the 6809 R/W line |
| 12 | I/O1 (bus D3) | D6 | PD6 | D3 |
| 13 | I/O2 (bus D2) | D7 | PD7 | D2 |
| 14 | I/O3 (bus D1) | D8 | PB0 | D1 |
| 15 | I/O4 (bus D0) | D9 | PB1 | D0 |
| 16 | A5 | D10 | PB2 | A5 |
| 17 | A6 | D11 | PB3 | A6 |
| 18 | Vcc | +5V | | +5V |

D0, D1, D12 and D13 are unused. D0 and D1 are the USB serial port. D13 drives
the on-board LED and is toggled by the bootloader on every reset.

**The table is indexed by pin number, not by signal name.** The chip's pins run
in order down the Arduino's — pin 1 to A0, pin 2 to A1, and so on — so the
signals do not: Arduino A0 carries address bit A7. The chip's address pins are
also out of numerical order on the package, with A0 through A5 on pins 6, 5, 4,
3, 2 and 16, A6 on 17 and A7 on pin 1.

Two of the instrument's net names invite a mismatch. The store strobe at
`$2000` is named `WE` with an overbar, and the 6809 R/W line, named `R`, is the
net that reaches the chip's `/WE` at pin 11. Matching net names to pin names
rather than reading pin numbers assigns `/CS`, `STORE` and `/WE` to the wrong
pins. On this wiring that places a driven Arduino pin on `STORE`, which
`writeAt()` pulses once per location: 256 store cycles per restore attempt,
each committing the current RAM contents. The pull-up offers no protection
against a line the sketch drives deliberately.

### Consequences for the sketch

Two properties of the sketch follow from this pin order:

- **The address bits sit on PORTC in reverse** — A0 on PC5 through A4 on PC1,
  with A7 on PC0 — and A5 and A6 are on PB2 and PB3. No mask-and-shift produces
  that mapping, so `setAddr()` scatters the bits individually.
- **PORTD carries all four control lines (PD2-PD5) and two of the four data
  lines (PD6, PD7).** A write of the form
  `PORTD = (PORTD & 0x0F) | (v << 4)` places the nibble in PD4-PD7, so
  `ARRAY RECALL` and `/WE` are driven with data bits on every nibble written.
  Every data access masks around PD2-PD5.

In compensation, all four control lines are single bits of one port. Each
transition is a single `cbi` or `sbi` instruction, with no read-modify-write,
which makes the early-write ordering in `writeAt()` exact.

### Data bus order

The instrument runs bus D0 to pin 15 and bus D3 to pin 12, which is chip I/O4
through I/O1. Bit order within a RAM is arbitrary provided it is consistent,
and the sketch follows the same convention, so a nibble read here carries the
value the instrument reads.

### Startup state

`busIdle()` writes the PORT latch before the direction register. `PORTB`,
`PORTC` and `PORTD` are zero out of reset, so setting a direction register
first would drive that pin low until the port register caught up — of the order
of 150-250 ns, which exceeds the 100 ns `tSTP` minimum and far exceeds the
roughly 20 ns rejected as noise. On `STORE` that is a valid store pulse, and it
would occur on every reset, including the DTR reset caused by opening the
serial port, ahead of any host-side check. Writing the latch first makes each
pin an input with its internal pull-up enabled during that interval, which
reinforces the external resistor rather than opposing it.

`novram.py` reads the sketch banner and requires **RW 3.0 or later**. Earlier
sketches expect `/CS` on PB2, `ARRAY RECALL` on PB3, `/WE` on PB4 and the data
nibble on PD4-PD7; on this wiring PD4 and PD5 are `ARRAY RECALL` and `/WE`, so
such a sketch drives two control lines with data patterns.

## Write protection in the sketch

There is one sketch and one wiring. `rw_x2212_uno.ino` refuses every write and
store until armed with an explicit token, is never armed by a read, refuses to
store unless the immediately preceding write verified, and permits one store
per arming. A backup transmits `R` and nothing else.

A physical write inhibit is available with one wire moved: with the Uno D2 lead
lifted and chip pin 9 strapped to +5V, the part cannot commit regardless of
software state. In that configuration a commit is reported as failed rather
than assumed, because the host issues an array recall and re-reads after the
store and finds the array unchanged.

## Data layout

The two devices share one address space at `$A000`. IC522 drives data bits 0-3
and holds the calibration block; IC523 drives bits 4-7 and holds the user
block. Within each device one byte occupies two consecutive locations, low
nibble first.

| Block | RAM | Bytes | Checksum | Device locations |
|---|---|---|---|---|
| Calibration | `$C300`-`$C359` | 90 | `$C358`-`$C359` over `$C300`-`$C357` | IC522, 180 |
| User | `$C35A`-`$C3AD` | 84 | `$C3AC`-`$C3AD` over `$C35A`-`$C3AB` | IC523, 168 |

Both checksums are 16-bit sums of successive words with the carry discarded,
the same arithmetic as the ROM checksum.

### User block

`$C35A` and `$C35B` are the tens and units digits of the IEEE-488 address, not
a packed configuration word. `SUB_F277` loads `$C35B` into A and `$C35A` into
B, adds ten to A B times, masks to five bits, and writes the result to the GPIB
controller at `$1004`:

    address = (10 x $C35A + $C35B) & $1F

The entry routine at `L_F7F2` rolls the previous units digit into `$C35A`,
clamped to 0-2 because 2x10+9 = 29 is the largest value surviving the mask. The
firmware default is `$C35A` = 0, `$C35B` = 9, giving address 9. Ten user
memories follow at `$C35C`, eight bytes each, which is the address `LDY #$C35C`
loads in the memory recall routine.

### Calibration block

| RAM | | |
|---|---|---|
| `$C300`-`$C326` | 13 constants, 3 bytes each | signed 24-bit big-endian, fixed point, **1.0 = `$400000`** |
| `$C327`-`$C356` | 6 setpoints, 8 bytes each | seven BCD digits in the low nibbles, one high nibble of 8 marking the decimal point, byte 7 a decade code. Same 8-byte shape as a user memory |
| `$C357` | 1 byte | inside the checksum, not written by the default loader. Purpose undetermined |
| `$C358`-`$C359` | checksum | |

`SUB_E1EE` reads a constant as `LDD ,U` plus `LDA 2,U` and tests its sign with
`TST ,U / BPL`, establishing a signed 24-bit big-endian quantity. On a scale of
`$400000` = 1.0, every ROM default is exactly 1.000000 and four of the five
distinct acceptance limits are exactly 2^-12, 0.1, 0.2 and 0.02. That fixes the
binary point at Q22.

The firmware carries its own nominal and tolerance tables for these constants.
`SUB_E0A1`, the routine that displays ` CAL DATA OK   `, walks the 13 constants
in steps of three and for each computes `|value - nominal|`, subtracts a limit,
and requires a negative result. Nominals are at `$EECE` and limits 89 bytes
further on at `$EF27`:

| idx | RAM | nominal | limit |
|---|---|---|---|
| 0 | `$C300` | +1.000000 | 0.000244 |
| 1-5 | `$C303`-`$C30F` | +1.000000 | 0.100000 |
| 6-9 | `$C312`-`$C31B` | +1.000000 | 0.200000 |
| 10 | `$C31E` | +0.216920 | 0.096154 |
| 11 | `$C321` | +0.666667 | 0.096154 |
| 12 | `$C324` | +0.003000 | 0.020000 |

`check` decodes all thirteen, prints them as numbers, and reports whether each
falls inside the window the instrument itself tests. This is a stronger
statement than the checksum alone: a checksum establishes only that the bytes
are self-consistent, whereas the acceptance test establishes that the
instrument would accept them as calibration. An image with a valid checksum and
constants outside these windows indicates a faulty read.

Which range or term each constant trims is not established. The uniform 1.0
defaults indicate multiplicative corrections, and the index arithmetic in
`SUB_E3E2` (`3 x $70` plus a 0-2 selector) indicates three terms per range
across four ranges, but this is inference and is not required for a byte-exact
restore.

### Setpoints at `$C327`

Each record carries seven BCD digits in the low nibbles of bytes 0-6, exactly
one high nibble set to 8 whose position gives the decimal point, and a decade
code in byte 7 that calibration does not alter.

Under this rule the six ROM defaults at `$EEF5` decode to exactly 100.0000,
1.000000, 10.00000, 100.0000, 1.000000 and 10.00000: six round nominals, one
per decade point. A calibrated instrument carries the same six values trimmed
by a few hundred ppm.

`check` decodes and prints them. The meaning of the decade codes in ohms is not
established; the sequence 0/1/1/1/2/2 against those nominals is consistent with
100R, 1k, 10k, 100k, 1M and 10M, but the routine applying them was not traced.

## The other two chips

IC520 and IC521 are **MM2114N-2L**, volatile static RAM of 1024 x 4 each. The
pair forms the 1 KB of working memory at `$C000`-`$C3FF`. Their contents are
lost at power-off and there is nothing to save. They confirm the memory map the
firmware implies: direct page `$C0`, with the calibration and user blocks at
the top of the same kilobyte.

## Operation

    python3 novram.py

With no arguments the tool presents a menu, enumerating serial ports and
offering backup, check and restore. The menu checks one image at a time; the
`check` subcommand takes any number of them. Every destructive step prompts first. The
same operations are available as the subcommands `read`, `check` and `write`;
`python3 novram.py --help` lists them.

There is one file format: 256 bytes, one byte per nibble, one file per device.
A backup is two such files. Nothing converts between formats because there is
nothing to convert to.

Every command that talks to the board takes the serial port as its first
argument. The tool enumerates the ports it can see and offers them by number,
so the port name rarely has to be typed. Where it does, the form depends on the
platform: `COM3` and similar on Windows, `/dev/ttyACM0` or `/dev/ttyUSB0` on
Linux, `/dev/cu.usbserial-*` on macOS. A board using a CH340 bridge rather than
the ATmega16U2 appears under a different description but behaves identically.

`flash.bat` compiles and uploads the sketch using the `arduino-cli` bundled
inside the Arduino IDE, without opening the IDE. Run with no argument it
compiles and lists the ports it can see; run with a port it uploads to that
port. It is a Windows convenience only — `arduino-cli` itself is
cross-platform, and the same two commands work on any host. Uploads are
performed with the socket empty, since the bootloader leaves every pin floating
for a second or more.

### Backup

Each device is read separately; the sketch reads whichever chip occupies the
socket. The menu asks which device is fitted, reads it, and reports whether the
result checks out as that block type, so a chip in the wrong socket is
identified immediately.

Each read comprises three passes by default, settable with `--passes`, which
must agree exactly; every 16-nibble record carries its own checksum. `check` recomputes block checksums as the firmware does. It accepts one or more
images and identifies each by its own checksum rather than by the order given,
so a calibration image is never decoded against the user block's layout or the
reverse.

An independent cross-check is available while the chips remain in the
instrument: `RM0` through `RM9` recall the ten user memories to the display,
and the display is what the GPIB talk buffer reports. `check` decodes the same
ten memories from IC523's nibble pairs. Agreement validates the pin mapping,
nibble order and unpacking against the instrument's own reading of the same
bytes. The cross-check is uninformative on an instrument whose user memories
are at their default of zero.

### Restore

The restore path enforces the following sequence:

1. Before the serial port is opened, the image is unpacked and its checksum
   tested against both block layouts. An image matching neither is refused, as
   is one matching both, since its type cannot then be established. The target
   device follows from the layout the image matches; it is not asked for.
2. The device is read before anything is armed, which establishes that the
   read path works on the current wiring and gives the dry run a reference
   image.
3. A dry run is mandatory. The image is sent as 16 checksummed records, written
   to the device's static RAM, read back and compared on the Arduino and again
   independently on the host. The tool then disarms and issues an array recall,
   reloading RAM from the untouched EEPROM, and compares that against the
   pre-read image. A failed dry run stops the procedure.
4. The commit prompt displays the source file and target device and requires
   the string `COMMIT` in capitals.
5. The commit issues one store cycle, the sketch disarms, and the host issues
   an array recall and re-reads. This confirms that the EEPROM array holds the
   data, not merely the RAM.

The sketch enforces its half independently: no write or store without the
arming token, no store without a verified immediately preceding write, one
store per arming.

After a restore, a power cycle of the instrument confirms it does not report
`CAL DATA BAD`.

`write` reads the part before arming anything. This establishes that the read
path works on the current wiring before any write is attempted, and gives the
dry run a reference image. The dry run ends by recalling and comparing against
that pre-read image; a mismatch aborts.

**Nothing establishes which device is physically in the socket.** The target
follows from the image's layout, and the write proceeds against whatever is
fitted. Verifying by hand that the right device is in the socket, and reading it
and comparing against a known backup beforehand, is the guard.

One failure mode is outside host-side detection. A store fired by the reset
that opening the serial port causes occurs before the tool has read anything,
leaving no reference image for comparison. The defences against it are the
`busIdle()` ordering, the two pull-ups, the `RECALL` pull-down and the sketch
version check.

## Store endurance

Store cycles are finite; reads are not. The firmware issues a store each time a
user memory is written, so a program looping on `SM<n>` consumes endurance
quickly. The restore path issues exactly one store per `--commit` and none on a
dry run.

## Files

| | |
|---|---|
| `rw_x2212_uno/rw_x2212_uno.ino` | The sketch. Reads, and once armed writes and stores. In its own folder as the Arduino IDE requires |
| `flash.bat` | Compiles and uploads via the `arduino-cli` bundled in the Arduino IDE. `flash.bat` compiles and lists the ports it can see; `flash.bat COMn` uploads to one of them. Windows only |
| `novram.py` | Host tool. No arguments for the menu; `read`, `check`, `write`, `pack` for scripting |
| `test_protocol.py` | Tests the serial path and menu against a simulated X2212 |

`test_protocol.py` models separate RAM and EEPROM arrays with recall and store
moving data between them, so it can establish that a dry run leaves the array
untouched, that a commit reaches it, and that a store the array does not accept
is detected rather than reported as success. It also drives the menu with
scripted input and confirms that each refusal leaves the array unchanged.

## Scope

`check` establishes that an image is intact, self-consistent, and inside the
firmware's acceptance windows. `write` restores it byte for byte. Neither
establishes what any individual constant means, nor whether the instrument was
correctly calibrated when the constants were written.
