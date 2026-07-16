#

from molecube_amaranth.config import Config
from molecube_amaranth.io import sma_pin

config = Config(TTLIN=sma_pin(0, 0),
                SPI_MOSI=sma_pin(1, 1), SPI_MISO=sma_pin(1, 2),
                SPI_SCLK=sma_pin(1, 3), SPI_CS=sma_pin(1, 4),
                CLOCK_HZ=230e6)
