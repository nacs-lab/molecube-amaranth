#

from dataclasses import dataclass

from . import io

# Change this version when making backward incompatible changes.
MAJOR_VERSION = 5
# Change this version when adding new features
MINOR_VERSION = 4

@dataclass(kw_only=True)
class Config:
    TTLIN: str = ''
    TTLOUT: str = ' '.join(io.ttl_bd_pin(fmc, idx) for fmc in range(2) for idx in range(28))
    TTLIO: str = ''
    CLOCKOUT: str = io.sma_pin(1, 0)

    SPI_MISO: str = ''
    SPI_MOSI: str = ''
    SPI_SCLK: str = ''
    SPI_CS: str = ''

    CLOCK_HZ: float = 200e6
    CLOCK_SHIFT: int = 1
    IOBUF_INSTANCE: bool = False

    # These numbers are in unit of half cycle, i.e. nominally 5ns
    DDS_WRITE_ADSU_2: int = 8 # Address/Data SetUp cycles
    DDS_WRITE_WRLOW_2: int = 8 # WRite enable LOW (assert) cycles
    DDS_WRITE_ADHD_2: int = 8 # Address/Data HolD cycles
    DDS_WRITE_FUDDL_2: int = 8 # FUD DeLay cycles
    DDS_WRITE_FUDHD_2: int = 8 # FUD HolD cycle

    DDS_READ_ASU_2: int = 23 # Address SetUp cycle
    DDS_READ_RDL_2: int = 16 # Read re-init DeLay cycle
    DDS_READ_RDHOZ_2: int = 21 # ReaD enable High to Output high-Z cycle

    DDS_RESET_RSHD_2: int = 32 # ReSet HolD cycle
