/* X2212 / NCR 52212 NOVRAM reader and writer -- Valhalla 2724A IC522 / IC523
 *
 * Arduino Uno (ATmega328P).  Direct connection, no shift registers.
 * 16 signals; A0-A5 and D2-D11.  D0/D1 (USB serial), D12 and D13 are free.
 *
 * WIRING -- "sequential by chip pin", the easy-to-build order.
 * The chip's pin numbers run in order down the Arduino's; the SIGNALS
 * therefore do not.  Chip pin 1 is A7, not A0.  Read this table by pin number,
 * never by signal name -- that is exactly the mistake that produced the
 * rotated map this project had to throw away.
 *
 *   chip  signal          Uno   AVR     note
 *   ----  --------------  ----  ------  -------------------------------------
 *     1   A7              A0    PC0
 *     2   A4              A1    PC1
 *     3   A3              A2    PC2
 *     4   A2              A3    PC3
 *     5   A1              A4    PC4
 *     6   A0              A5    PC5
 *     7   /CS             D3    PD3
 *     8   GND             GND
 *     9   STORE           D2    PD2     + 10k to +5V   MANDATORY
 *    10   ARRAY RECALL    D4    PD4     + 10k to GND   recommended
 *    11   /WE             D5    PD5     + 10k to +5V   MANDATORY
 *    12   I/O1  (bus D3)  D6    PD6
 *    13   I/O2  (bus D2)  D7    PD7
 *    14   I/O3  (bus D1)  D8    PB0
 *    15   I/O4  (bus D0)  D9    PB1
 *    16   A5              D10   PB2
 *    17   A6              D11   PB3
 *    18   Vcc             +5V
 *
 * Pin map verified three ways: drawing 2724-070 sheet 5, X2212.pdf and
 * NCR52212.pdf.  /CS is pin 7, STORE is pin 9, ARRAY RECALL is pin 10,
 * /WE is pin 11.
 *
 * WHAT THIS WIRING DOES TO THE PORT ARITHMETIC
 *
 * Nothing lands anywhere convenient, and two things are actively hostile:
 *
 *   1. The address bits sit on PORTC BACKWARDS -- A0 on PC5 down to A4 on PC1,
 *      with A7 on PC0 -- and A5/A6 are over on PB2/PB3.  There is no mask and
 *      shift that produces that; setAddr() scatters the bits one at a time.
 *   2. PORTD carries the four CONTROL lines (PD2-PD5) as well as two of the
 *      four data lines (PD6, PD7).  A write of the form
 *          PORTD = (PORTD & 0x0F) | (v << 4)
 *      places the nibble in PD4-PD7, so ARRAY RECALL and /WE would be driven
 *      with data bits on every nibble written.  Every data access here masks
 *      around PD2-PD5.  Do not "simplify" it.
 *
 * The consolation is that all four control lines are now single bits of one
 * port, so each is one cbi/sbi instruction -- no read-modify-write, and the
 * early-write ordering in writeAt() is exact to the instruction.
 *
 * ---------------------------------------------------------------------------
 * THE PULL-UPS ARE NOT OPTIONAL.
 *
 * /WE (D5) and STORE (D2) are both active low.  Every Arduino pin is a
 * high-impedance input at power-up, during reset, and throughout a sketch
 * upload -- the bootloader leaves them alone for a second or more.  Without a
 * physical pull-up on each of these two lines they float through all three
 * windows, and a floating STORE can commit whatever happens to be in the
 * volatile RAM into the EEPROM array.
 *
 * Fit 10k from D5 to +5V and 10k from D2 to +5V before connecting the chip.
 * 10k from D4 (ARRAY RECALL) to GND is a second, independent store inhibit and
 * is worth fitting too: both datasheets say holding STORE high OR RECALL low
 * prevents an unintentional store, and a low recall only ever copies EEPROM
 * into RAM.
 * ---------------------------------------------------------------------------
 *
 * Writing is staged and must be unlocked:
 *
 *   I                 identify
 *   R                 array recall, then read 256 nibbles.  Always available,
 *                     and the only thing a backup sends.  Issued after a store
 *                     it is also what proves the EEPROM took the data.
 *   A WRITE-ENABLE    arm; required before W or S
 *   W                 write 256 nibbles to RAM, then dump RAM back for the
 *                     host to verify.  Does NOT touch the EEPROM array.
 *   S                 commit RAM to the EEPROM array.  Refused unless the
 *                     immediately preceding W verified.  Auto-disarms.
 *   D                 disarm
 *
 * Writing RAM without S is harmless: the instrument issues an array recall at
 * power-up, which overwrites RAM from the EEPROM array.
 *
 * TAKING A BACKUP WITH THIS SKETCH.  There is no separate read-only sketch any
 * more.  R and V never arm anything and cannot write, but that is a software
 * guarantee, not a physical one.  For the one irreplaceable operation -- the
 * first backup -- consider lifting the D2 wire and strapping chip pin 9 to +5V
 * instead.  It is one wire, it restores the hard guarantee, and if you forget
 * to put it back the host's post-store verify catches it: the array will not
 * have changed and the commit is reported as failed.
 *
 * Record format (both directions): AA + 16 hex nibbles + SS
 *   AA = start address, SS = 8-bit sum of the 16 nibble values
 */
/* Host and sketch share a major version: the command set below is the contract
   between them, so a change to it is a major bump.  novram.py parses this
   banner and refuses anything it does not recognise or that is older than it
   requires. */
#define VERSION "1.0.0"
#define BANNER  "X2212-NOVRAM v" VERSION
#define ARMTOKEN "WRITE-ENABLE"

/* ---- control, all four on PORTD ---------------------------------------- */
#define ST_BIT   (1 << 2)        /* PD2, Uno D2              -> chip pin 9  */
#define CS_BIT   (1 << 3)        /* PD3, Uno D3              -> chip pin 7  */
#define RE_BIT   (1 << 4)        /* PD4, Uno D4              -> chip pin 10 */
#define WE_BIT   (1 << 5)        /* PD5, Uno D5              -> chip pin 11 */
#define CTRL_MASK (ST_BIT | CS_BIT | RE_BIT | WE_BIT)

/* ---- data, split across two ports -------------------------------------- */
#define DAT_D_MASK 0xC0          /* PD6 = bus D3, PD7 = bus D2 */
#define DAT_B_MASK 0x03          /* PB0 = bus D1, PB1 = bus D0 */

/* ---- address ------------------------------------------------------------ */
#define ADDR_C_MASK 0x3F         /* PC0=A7, PC1=A4, PC2=A3, PC3=A2, PC4=A1, PC5=A0 */
#define ADDR_B_MASK 0x0C         /* PB2 = A5, PB3 = A6 */

/* Conservative timings, sized against the NCR 52212 datasheet -- the part
   actually fitted -- not the Xicor X2212 it second-sources.  They differ where
   it matters:

     parameter                  Xicor X2212      NCR 52212
     read cycle      tRC          300 ns           300 ns
     write pulse     tWP          150 ns           150 ns
     store pulse     tSTP         100 ns           100 ns
     store time      tSTC          10 ms max        10 ms max
     recall pulse    tRCP         450 ns           200 ns
     RECALL CYCLE    tRCC        1200 ns        30 us typ, 70 us MAX
     store cycles    NSC       10k (100k on /10)   10k

   tRCC is the one that bites: the NCR part can take up to seventy MICROseconds
   to finish copying the array into RAM.  Read too soon and you get a
   half-recalled RAM -- and because every pass recalls the same way, three
   passes would agree with each other and the host would call it good.
   150 us on each side of the pulse puts us at 2x the worst case. */
#define T_ACCESS_US    3
#define T_WRITE_US     3
#define T_RECALL_US  150
#define T_STORE_MS    25

static bool armed = false;
static bool lastWriteVerified = false;
static uint8_t buf[256];

/* ------------------------------------------------------------------ bus --- */
/* Only PD6/PD7 and PB0/PB1 are touched.  The read-modify-writes below are safe
   against the UART interrupt, which touches UDR0 and its ring buffer, never a
   port register. */
static void dataInput(void) {
  DDRD  &= (uint8_t)~DAT_D_MASK;
  PORTD &= (uint8_t)~DAT_D_MASK;           /* no pull-ups, control untouched */
  DDRB  &= (uint8_t)~DAT_B_MASK;
  PORTB &= (uint8_t)~DAT_B_MASK;           /* address bits PB2/PB3 untouched */
}

static void dataOutput(void) {
  DDRD |= DAT_D_MASK;
  DDRB |= DAT_B_MASK;
}

/* bus D0 -> PB1, D1 -> PB0, D2 -> PD7, D3 -> PD6.
   Reversed within each port, which is why this is bit-by-bit. */
static void putNibble(uint8_t v) {
  uint8_t b = (uint8_t)(PORTB & ~DAT_B_MASK);
  if (v & 0x01) b |= (1 << 1);
  if (v & 0x02) b |= (1 << 0);
  PORTB = b;
  uint8_t d = (uint8_t)(PORTD & ~DAT_D_MASK);
  if (v & 0x04) d |= (1 << 7);
  if (v & 0x08) d |= (1 << 6);
  PORTD = d;
}

/* Both ports are sampled once, then decoded, so all four bits come from the
   same instant rather than from two reads a few cycles apart. */
static uint8_t getNibble(void) {
  uint8_t pb = PINB, pd = PIND, v = 0;
  if (pb & (1 << 1)) v |= 0x01;            /* I/O4, chip pin 15 -> bus D0 */
  if (pb & (1 << 0)) v |= 0x02;            /* I/O3, chip pin 14 -> bus D1 */
  if (pd & (1 << 7)) v |= 0x04;            /* I/O2, chip pin 13 -> bus D2 */
  if (pd & (1 << 6)) v |= 0x08;            /* I/O1, chip pin 12 -> bus D3 */
  return v;
}

/* A0->PC5, A1->PC4, A2->PC3, A3->PC2, A4->PC1, A7->PC0, A5->PB2, A6->PB3. */
static void setAddr(uint8_t a) {
  uint8_t c = 0;
  if (a & 0x01) c |= (1 << 5);             /* A0 */
  if (a & 0x02) c |= (1 << 4);             /* A1 */
  if (a & 0x04) c |= (1 << 3);             /* A2 */
  if (a & 0x08) c |= (1 << 2);             /* A3 */
  if (a & 0x10) c |= (1 << 1);             /* A4 */
  if (a & 0x80) c |= (1 << 0);             /* A7 */
  PORTC = (uint8_t)((PORTC & ~ADDR_C_MASK) | c);
  uint8_t b = (uint8_t)(PORTB & ~ADDR_B_MASK);
  if (a & 0x20) b |= (1 << 2);             /* A5 */
  if (a & 0x40) b |= (1 << 3);             /* A6 */
  PORTB = b;
}

/* Bring the control lines up SAFE.
 *
 * PORTB, PORTC and PORTD are 0x00 out of reset.  Setting DDRx first therefore
 * turns /WE and STORE into outputs driving LOW, and they stay low for the two
 * or three instructions until PORTx catches up -- of the order of 150-250 ns.
 * tSTP minimum is 100 ns and the noise rejection only swallows pulses under
 * about 20 ns, so that glitch is a perfectly valid store pulse: it commits
 * whatever indeterminate junk is in the volatile RAM straight into the array,
 * on every reset, including the DTR reset the host causes just by opening the
 * serial port.  The external pull-ups cannot help -- the pin is being driven,
 * not left floating.
 *
 * Writing the PORT latch FIRST, while DDRx is still 0, makes each pin an input
 * with its internal pull-up enabled, which reinforces the external 10k rather
 * than fighting it.  Enabling the driver afterwards is then a high-to-high
 * transition and no glitch exists. */
static void busIdle(void) {
  PORTD |= CTRL_MASK;                      /* latch all four high FIRST */
  DDRD  |= CTRL_MASK;                      /* then enable the drivers   */
  DDRC  |= ADDR_C_MASK;                    /* address outputs */
  DDRB  |= ADDR_B_MASK;
  dataInput();
}

static uint8_t readAt(uint8_t a) {
  dataInput();
  setAddr(a);
  PORTD &= (uint8_t)~CS_BIT;
  delayMicroseconds(T_ACCESS_US);
  uint8_t v = getNibble();
  PORTD |= CS_BIT;
  return v;
}

/* RAM write, as an EARLY WRITE cycle: /WE falls before /CS.
 *
 * The order matters.  Taking /CS low first puts the part in read mode for as
 * long as it takes the next instruction to execute -- 125 ns on a 16 MHz Uno --
 * while we are driving the same four I/O lines.  tA (chip select to data
 * active) has a minimum of 0 ns, so the chip is entitled to start driving
 * immediately and the two ends fight.  The NCR datasheet spells the cure out
 * under its write-cycle diagram: "WE may be asserted prior to CS.  tAS applies
 * to CS or WE whichever occurs last."  With /WE already low when /CS falls the
 * outputs never turn on at all -- Xicor draw exactly this as the "Early Write
 * Cycle", DATA OUT held in high-Z for the whole cycle.
 *
 * Both lines are single bits of PORTD, so each transition below is one cbi or
 * sbi and the ordering is exact.
 *
 * Address and data are set up T_WRITE_US before either control line falls
 * (tAS min 50 ns, tDW min 150 ns) and held T_WRITE_US after both have risen
 * (tDH min 20 ns, tWR min 25 ns). */
static void writeAt(uint8_t a, uint8_t v) {
  setAddr(a);
  dataOutput();
  putNibble(v);
  delayMicroseconds(T_WRITE_US);           /* tAS, tDW setup      */
  PORTD &= (uint8_t)~WE_BIT;               /* /WE first: no contention */
  PORTD &= (uint8_t)~CS_BIT;               /* then select         */
  delayMicroseconds(T_WRITE_US);           /* tWP  min 150 ns     */
  PORTD |= CS_BIT;                         /* end of write, tCW   */
  PORTD |= WE_BIT;                         /* tWR  min  25 ns     */
  delayMicroseconds(T_WRITE_US);           /* tDH  min  20 ns     */
  dataInput();
}

static void arrayRecall(void) {
  PORTD |= CS_BIT;                         /* keep the chip deselected */
  PORTD &= (uint8_t)~RE_BIT;
  delayMicroseconds(T_RECALL_US);          /* tRCP min 200 ns */
  PORTD |= RE_BIT;
  delayMicroseconds(T_RECALL_US);          /* tRCC up to 70 us on the NCR part */
}

/* Commit RAM to the EEPROM array.
   RECALL is driven high explicitly, not merely assumed to be: both datasheets
   state that a recall in progress INHIBITS the store, and NCR add "to ensure a
   valid Store or Recall cycle, do not apply STORE and RECALL at the same
   time".  A store silently swallowed because RECALL happened to be low is the
   one failure that would look like success. */
static void arrayStore(void) {
  PORTD |= (uint8_t)(CS_BIT | WE_BIT | RE_BIT);   /* deselected, no write, no recall */
  delayMicroseconds(2);                    /* let RECALL settle high */
  PORTD &= (uint8_t)~ST_BIT;
  delayMicroseconds(50);                   /* tSTP min 100 ns */
  PORTD |= ST_BIT;
  delay(T_STORE_MS);                       /* tSTC max 10 ms  */
}

/* ---------------------------------------------------------------- serial -- */
static void hex1(uint8_t v) { Serial.print("0123456789ABCDEF"[v & 0x0F]); }
static void hex2(uint8_t v) { hex1(v >> 4); hex1(v); }

static void dumpAll(void) {
  for (uint16_t base = 0; base < 256; base += 16) {
    uint8_t sum = 0;
    hex2((uint8_t)base);
    for (uint8_t i = 0; i < 16; i++) {
      uint8_t v = readAt((uint8_t)(base + i));
      sum = (uint8_t)(sum + v);
      hex1(v);
    }
    hex2(sum);
    Serial.println();
  }
  Serial.println("END");
}

static int hexv(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

/* Read one line into s, max n-1 chars.  Returns length, or -1 on timeout. */
static int readLine(char *s, uint8_t n) {
  uint8_t i = 0;
  unsigned long t0 = millis();
  for (;;) {
    if (millis() - t0 > 5000) return -1;
    if (!Serial.available()) continue;
    char c = Serial.read();
    if (c == '\n') { s[i] = 0; return i; }
    if (c == '\r') continue;
    if (i < n - 1) s[i++] = c;
  }
}

/* Every one of the 256 locations must be supplied by this transfer.  buf is
   static, so accepting sixteen copies of record 00 would leave 240 locations
   holding whatever the PREVIOUS transfer put there and then write that to the
   part.  Requiring record n to carry address n*16 makes full, in-order
   coverage the only thing that can get through. */
static bool receiveImage(void) {
  char line[24];
  for (uint16_t i = 0; i < 256; i++) buf[i] = 0;
  Serial.println("SEND");
  for (uint8_t rec = 0; rec < 16; rec++) {
    int len = readLine(line, sizeof(line));
    if (len != 20) { Serial.println("ERR format"); return false; }
    int b1 = hexv(line[0]), b0 = hexv(line[1]);
    if (b1 < 0 || b0 < 0) { Serial.println("ERR addr"); return false; }
    uint8_t base = (uint8_t)((b1 << 4) | b0);
    if (base != (uint8_t)(rec * 16)) { Serial.println("ERR order"); return false; }
    uint8_t sum = 0;
    uint8_t vals[16];
    for (uint8_t i = 0; i < 16; i++) {
      int v = hexv(line[2 + i]);
      if (v < 0) { Serial.println("ERR data"); return false; }
      vals[i] = (uint8_t)v;
      sum = (uint8_t)(sum + v);
    }
    int s1 = hexv(line[18]), s0 = hexv(line[19]);
    if (s1 < 0 || s0 < 0 || (uint8_t)((s1 << 4) | s0) != sum) {
      Serial.println("ERR checksum"); return false;
    }
    for (uint8_t i = 0; i < 16; i++) buf[base + i] = vals[i];
  }
  return true;
}

void setup(void) {
  busIdle();
  Serial.begin(115200);
  while (!Serial) { }
  Serial.println(BANNER);
}

void loop(void) {
  if (!Serial.available()) return;
  char c = Serial.read();
  if (c == '\r' || c == '\n') return;

  if (c == 'I') {
    Serial.println(BANNER);

  } else if (c == 'R') {
    busIdle();
    arrayRecall();
    dumpAll();

  } else if (c == 'A') {
    char line[24];
    if (readLine(line, sizeof(line)) < 0) { Serial.println("ERR timeout"); return; }
    char *p = line;
    while (*p == ' ') p++;
    if (strcmp(p, ARMTOKEN) == 0) { armed = true; Serial.println("ARMED"); }
    else { armed = false; Serial.println("LOCKED"); }

  } else if (c == 'D') {
    armed = false; lastWriteVerified = false;
    Serial.println("DISARMED");

  } else if (c == 'W') {
    lastWriteVerified = false;
    if (!armed) { Serial.println("LOCKED"); return; }
    if (!receiveImage()) return;
    busIdle();
    for (uint16_t i = 0; i < 256; i++) writeAt((uint8_t)i, buf[i]);
    bool ok = true;
    for (uint16_t i = 0; i < 256; i++)
      if (readAt((uint8_t)i) != buf[i]) { ok = false; break; }
    lastWriteVerified = ok;
    Serial.println(ok ? "WROTE" : "ERR verify");
    dumpAll();                             /* host compares independently */

  } else if (c == 'S') {
    if (!armed) { Serial.println("LOCKED"); return; }
    if (!lastWriteVerified) { Serial.println("ERR no verified write"); return; }
    arrayStore();
    armed = false;                         /* one store per arming */
    lastWriteVerified = false;
    Serial.println("STORED");
  }
}
