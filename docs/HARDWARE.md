# Coin acceptor, GPIO and safe wiring

**Nothing here has been verified on your hardware.** Coin input stays disabled (`coin.hardware_verified=false`) until you do the checks.

## 1. Pin identities (do not mix them up)
| Name | Value for physical pin 3 |
|---|---|
| Physical header pin | 3 |
| H3 SoC pad | PA12 (alternate function TWI0-SDA, I2C0 data) |
| Linux GPIO number (sunxi: bank A = 0) | 12 (expected; confirm with `sudo cat /sys/kernel/debug/gpio` or `gpioinfo`) |
| libgpiod | chip `gpiochip0` (the H3 main pin controller, confirm), line 12 |
Pin 5 is PA11 (I2C0 clock). Many community builds use pin 3 because it is convenient, but it is an I2C pin: check that I2C0 is not enabled
(`ls /dev/i2c-*`, `grep overlays /boot/armbianEnv.txt`, remove `i2c0` from overlays if present) and check the board's pull-up on that pad
before choosing it. The Orange Pi One has a 40-pin header whose first 26 pins follow the older layout; confirm the numbering against the
board silkscreen and the official Orange Pi One schematic. If anything is unclear, choose a plain GPIO instead.

## 2. Before touching a wire
1. Identify the acceptor model and get its datasheet (many are "multi coin" 4-6 wire units: +12 V, GND, COIN, sometimes COUNTER).
2. With a multimeter on the unit powered at 12 V (and no Pi connected), measure the COIN wire to GND while idle and while a coin passes:
   idle level, pulse level, polarity (rest high -> pulses low, or rest low -> pulses high), pulse width.
3. Check for an open-collector output (floats high only through a pull-up) versus a driven output.
4. Decide the pulses per coin in the acceptor's programming mode and put the same mapping in the admin panel (Rates -> Coin pulse mapping).

## 3. Protected interface (starting point, to be sized for your measurements)
The H3 GPIO is **3.3 V only and not 5 V tolerant**. Never connect the 12 V acceptor signal directly. Use an optocoupler (e.g. PC817) so the
acceptor's 12 V side and the Pi are electrically isolated:

```
12 V side                                   Pi side
COIN pin ---[R ~1.2-1.5 kΩ]---> opto LED(+)    opto transistor collector ---> GPIO pin (PA12 or chosen pin)
12 V GND <-----------------------opto LED(-)    opto emitter -------------------> Pi GND
                                                GPIO pin --- internal or 10 kΩ pull-up to 3V3
```
- If COIN is a driven 12 V pulse: LED(+) via R from COIN, LED(-) to the acceptor GND. R = (12 V - 1.2 V) / ~8 mA ≈ 1.3 kΩ.
- If COIN is open-collector to GND (pulls low): connect LED(+) through R to the acceptor +12 V and LED(-) to the COIN pin.
- The GPIO then sits at 3.3 V idle and goes low during a pulse. This is why `coin.active_low` defaults to true and edges are read on the logical
  rising edge. If the pulse is shorter than ~20 ms, lower `coin.debounce_ms`. Check pulse width on a scope or logic analyser if you have one.
- Keep the acceptor on its own 12 V supply; common-ground the two sides only through the opto-isolated path.

## 3b. Testing the whole system before wiring
Set `coin.test_mode` to `true` in `/etc/jp-pisowifi/config.json` and restart `jp-pisowifi` and `jp-pisowifi-coin`. The portal then shows simulated coin buttons while the real firewall, DHCP and speed limits stay active. Anyone who can open the portal can press them for free time, so use it only while you test, then set it back to `false`. It switches itself off once `hardware_verified` is true.

## 4. Enabling GPIO in software
1. `sudo jp-pisowifi` services are installed; the coin service exits with code 3 until configured.
2. Edit `/etc/jp-pisowifi/config.json`: `"gpio_chip": "gpiochip0"`, `"gpio_line": <number>`, `"hardware_verified": true`.
3. `sudo systemctl restart jp-pisowifi-coin` and watch `journalctl -u jp-pisowifi-coin -f`; the admin Sales page lists every coin event.
4. Test with one coin of each denomination and confirm the pulse count matches your mapping. Test coin-spam and power-cycle during a coin.
