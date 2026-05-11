#

from molecube_amaranth.config import Config
from molecube_amaranth.io import sma_pin, ttl_bd_pin
import dataclasses

ttl_pins = []
for idx in range(28):
    if idx == 0:
        ttl_pins.append(sma_pin(0, 0))
    elif idx == 4:
        ttl_pins.append(sma_pin(0, 1))
    elif idx == 8:
        ttl_pins.append(sma_pin(0, 2))
    elif idx == 12:
        ttl_pins.append(sma_pin(0, 3))
    elif idx == 16:
        ttl_pins.append(sma_pin(1, 1))
    elif idx == 20:
        ttl_pins.append(sma_pin(1, 2))
    elif idx == 24:
        ttl_pins.append(sma_pin(1, 3))
    else:
        ttl_pins.append(ttl_bd_pin(1, idx))

ttlin_pins = [ttl_bd_pin(1, 24), ttl_bd_pin(0, 24)]

config = Config(TTLIN=' '.join(ttlin_pins), TTLOUT=' '.join(ttl_pins))
config100 = dataclasses.replace(config, CLOCK_HZ=100e6, CLOCK_SHIFT=0)
