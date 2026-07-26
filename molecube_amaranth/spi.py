#

from amaranth import *
from amaranth.lib.data import StructLayout

from transactron import TModule, Transaction, Method, def_method

from .utils import assign_xvalue, oring_combiner

class SPIController(Elaboratable):
    def __init__(self, spiio, result_fifo, *, div_width=9):
        self.spiio = spiio
        self.div_width = div_width
        self.spi_layout = StructLayout(dict(
            # Data to be sent on SPI, MSB first
            data=32,
            # clock divider for SPI output clock: spi_sclk = clock / (2 * (div + 1))
            div=div_width,
            # nbits_minus_1 + 1 is number of bits
            nbits_minus_1=5,
            # Whether we return the result
            result=1,
            id=2,
            clk_pha=1,
            clk_pol=1))
        self.result_fifo = result_fifo
        self.set = Method(i=self.spi_layout)
        self.busy = Signal()

    def elaborate(self, plat):
        m = TModule()

        # Data received on SPI, MSB first
        result_data = Signal(32, reset_less=True)

        # Sending/receiving
        status = Signal(self.spi_layout, reset_less=True)
        # Chip selection, active high
        spi_cs = Signal(4)
        # SPI clock
        spi_sclk = Signal(1, init=1)
        spi_sclk_edges = Signal(range(2 * 32 + 1), reset_less=True)

        div_cycle = Signal(self.div_width, reset_less=True)

        spiio = self.spiio

        if spiio is not None:
            m.d.comb += [spiio.mosi.o.eq(status.data[31]),
                         spiio.cs.o.eq(~spi_cs),
                         spiio.sclk.o.eq(spi_sclk)]

        @def_method(m, self.set, combiner=oring_combiner, nonexclusive=True)
        def _(arg):
            assign_xvalue(m, spi_sclk_edges)
            assign_xvalue(m, div_cycle)
            m.d.sync += [self.busy.eq(1),
                         status.eq(arg),
                         # Set up the clock first before asserting chip select
                         # since we might not be idling in the correct clock level
                         spi_sclk.eq(arg.clk_pol)]

        final_result = Signal(32, reset_less=True)
        write_result = Signal(1)
        assign_xvalue(m, final_result)

        falling_output = Signal(1, reset_less=True)
        m.d.sync += falling_output.eq(status.clk_pha^status.clk_pol)

        with m.If(self.busy):
            with m.If(spi_cs == 0):
                # Setup
                m.d.sync += [spi_cs.eq(1 << status.id),
                             spi_sclk_edges.eq(0),
                             div_cycle.eq(status.div),
                             result_data.eq(0)]
            with m.Elif(div_cycle != 0):
                m.d.sync += div_cycle.eq(div_cycle - 1)
            with m.Else():
                # spi_sclk = 0 means this is a rising edge
                # spi_sclk = 1 means this is a falling edge
                # falling_output == 0 means data is valid on rising clock edges
                # falling_output == 1 means data is valid on falling clock edges
                # Update spi_mosi.  Data is valid on next clock edge.
                with m.If(spi_sclk != falling_output):
                    with m.If(spi_sclk_edges != 0):
                        m.d.sync += status.data.eq(status.data << 1)
                with m.Else():
                    # read in data when it is valid
                    if spiio is not None:
                        m.d.sync += result_data.eq((result_data << 1) | spiio.miso.i)

                m.d.sync += [div_cycle.eq(status.div),
                             spi_sclk_edges.eq(spi_sclk_edges + 1)]
                with m.If((spi_sclk_edges >> 1) == status.nbits_minus_1 + 1):
                    assign_xvalue(m, spi_sclk_edges)
                    assign_xvalue(m, div_cycle)
                    assign_xvalue(m, status)
                    m.d.sync += [self.busy.eq(0),
                                 spi_cs.eq(0),
                                 spi_sclk.eq(1),
                                 status.data[31].eq(0)]
                    with m.If(status.result):
                        m.d.sync += [write_result.eq(1),
                                     final_result.eq(result_data)]
                with m.Else():
                    m.d.sync += spi_sclk.eq(~spi_sclk)
        with m.Else():
            assign_xvalue(m, spi_sclk_edges)
            assign_xvalue(m, div_cycle)

        with Transaction().body(m, ready=write_result):
            self.result_fifo.write(m, final_result)
            m.d.sync += write_result.eq(0)

        return m
