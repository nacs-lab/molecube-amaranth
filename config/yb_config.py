#

from molecube_amaranth.config import Config
from molecube_amaranth.io import sma_pin, ttl_bd_pin
import dataclasses

ttl_pins = []
for bank in range(2):
    for idx in range(28):
        if idx != 24:
            ttl_pins.append(ttl_bd_pin(0 if bank == 1 else 1, idx))
        elif bank == 0:
            ttl_pins.append(sma_pin(1, 3))
        else:
            ttl_pins.append(sma_pin(1, 2))

ttlin_pins = [ttl_bd_pin(1, 24), ttl_bd_pin(0, 24), sma_pin(0, 4), sma_pin(0, 0)]

config = Config(TTLIN=' '.join(ttlin_pins), TTLOUT=' '.join(ttl_pins))
config100 = dataclasses.replace(config, CLOCK_HZ=100e6, CLOCK_SHIFT=0)
config100_bufinst = dataclasses.replace(config100, IOBUF_INSTANCE=True)
